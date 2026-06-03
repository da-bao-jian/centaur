import { describe, expect, it, mock } from 'bun:test'
import { handleSocketModeSlackEvent, startSlackSocketMode } from './socket-mode'
import type { SlackEnvelope } from './types'

describe('Slack Socket Mode', () => {
  it('does not start without an app-level token', async () => {
    const createClient = mock(() => {
      throw new Error('should not create a socket client')
    })

    const client = await startSlackSocketMode({
      appToken: undefined,
      processEnvelope: async () => {},
      createClient
    })

    expect(client).toBeNull()
    expect(createClient).not.toHaveBeenCalled()
  })

  it('starts a socket client and registers Slack event handling', async () => {
    let slackEventHandler:
      | ((event: { body?: SlackEnvelope; ack?: () => Promise<void> }) => Promise<void>)
      | undefined
    const client = {
      on: mock((eventName: string, handler: unknown) => {
        if (eventName === 'slack_event') {
          slackEventHandler = handler as typeof slackEventHandler
        }
      }),
      start: mock(async () => {})
    }
    const createClient = mock(() => client)
    const processEnvelope = mock(async (_envelope: SlackEnvelope) => {})

    const started = await startSlackSocketMode({
      appToken: 'xapp-test-token',
      processEnvelope,
      createClient
    })

    expect(started).toBe(client)
    expect(createClient).toHaveBeenCalledWith({
      appToken: 'xapp-test-token',
      slackApiUrl: undefined
    })
    expect(client.on).toHaveBeenCalledWith('slack_event', expect.any(Function))
    expect(client.on).toHaveBeenCalledWith('error', expect.any(Function))
    expect(client.start).toHaveBeenCalledTimes(1)
    expect(slackEventHandler).toBeDefined()
  })

  it('acks Socket Mode events before processing the Slack envelope', async () => {
    const calls: string[] = []
    const envelope: SlackEnvelope = {
      type: 'event_callback',
      event_id: 'Ev-socket',
      team_id: 'T123',
      event: {
        type: 'app_mention',
        user: 'U123',
        channel: 'C123',
        ts: '1780000000.000100',
        text: '<@UBOT> hello'
      }
    }

    await handleSocketModeSlackEvent(
      {
        body: envelope,
        ack: async () => {
          calls.push('ack')
        }
      },
      async received => {
        calls.push(`process:${received.event_id}`)
      }
    )

    expect(calls).toEqual(['ack', 'process:Ev-socket'])
  })

  it('still processes the envelope when ack throws', async () => {
    const processEnvelope = mock(async (_envelope: SlackEnvelope) => {})
    const envelope: SlackEnvelope = {
      type: 'event_callback',
      event_id: 'Ev-ack-failed',
      team_id: 'T123',
      event: {
        type: 'app_mention',
        user: 'U123',
        channel: 'C123',
        ts: '1780000000.000200',
        text: '<@UBOT> hello'
      }
    }

    await handleSocketModeSlackEvent(
      {
        body: envelope,
        ack: async () => {
          throw new Error('ack failed')
        }
      },
      processEnvelope
    )

    expect(processEnvelope).toHaveBeenCalledWith(envelope)
  })
})
