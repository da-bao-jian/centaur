import { logError, logInfo, logWarn } from '../logging'
import type { SlackEnvelope } from './types'

export type SocketModeClientLike = {
  on(eventName: string, listener: (event: unknown) => unknown): unknown
  start(): Promise<unknown> | unknown
}

type SocketModeClientOptions = {
  appToken: string
  slackApiUrl?: string
}

export type CreateSocketModeClient = (opts: SocketModeClientOptions) => SocketModeClientLike

type SocketModeSlackEvent = {
  body?: unknown
  ack?: () => Promise<void> | void
}

export async function startSlackSocketMode(opts: {
  appToken?: string
  slackApiUrl?: string
  processEnvelope: (envelope: SlackEnvelope) => Promise<void>
  createClient?: CreateSocketModeClient
}): Promise<SocketModeClientLike | null> {
  if (!opts.appToken) return null

  const createClient =
    opts.createClient ??
    ((clientOpts: SocketModeClientOptions) => new RawSlackSocketModeClient(clientOpts))
  const client = createClient({ appToken: opts.appToken, slackApiUrl: opts.slackApiUrl })

  client.on('slack_event', event => {
    void handleSocketModeSlackEvent(event as SocketModeSlackEvent, opts.processEnvelope).catch(
      error => {
        logError('slack_socket_mode_event_processing_failed', error)
      }
    )
  })
  client.on('error', error => {
    logError('slack_socket_mode_error', error)
  })

  await client.start()
  logInfo('slack_socket_mode_started')
  return client
}

export async function handleSocketModeSlackEvent(
  event: SocketModeSlackEvent,
  processEnvelope: (envelope: SlackEnvelope) => Promise<void>
): Promise<void> {
  try {
    await event.ack?.()
  } catch (error) {
    logError('slack_socket_mode_ack_failed', error)
  }

  if (!isSlackEnvelope(event.body)) {
    logWarn('slack_socket_mode_invalid_event_body', {
      body_type: typeof event.body
    })
    return
  }

  await processEnvelope(event.body)
}

function isSlackEnvelope(value: unknown): value is SlackEnvelope {
  return Boolean(
    value && typeof value === 'object' && typeof (value as SlackEnvelope).type === 'string'
  )
}

type Listener = (event: unknown) => unknown

type RawSocketEnvelope = {
  envelope_id?: string
  payload?: unknown
  type?: string
}

type AppsConnectionsOpenResponse = {
  ok?: boolean
  url?: string
  error?: string
}

class RawSlackSocketModeClient implements SocketModeClientLike {
  private readonly appToken: string
  private readonly slackApiUrl?: string
  private readonly listeners = new Map<string, Listener[]>()
  private socket: WebSocket | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectDelayMs = 1_000
  private closed = false

  constructor(opts: SocketModeClientOptions) {
    this.appToken = opts.appToken
    this.slackApiUrl = opts.slackApiUrl
  }

  on(eventName: string, listener: Listener): void {
    const listeners = this.listeners.get(eventName) ?? []
    listeners.push(listener)
    this.listeners.set(eventName, listeners)
  }

  async start(): Promise<void> {
    this.closed = false
    try {
      await this.connect()
    } catch (error) {
      this.emit('error', error)
      this.scheduleReconnect()
    }
  }

  private async connect(): Promise<void> {
    const url = await openSocketModeUrl({
      appToken: this.appToken,
      slackApiUrl: this.slackApiUrl
    })

    await new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(url)
      this.socket = socket

      socket.addEventListener('open', () => {
        this.reconnectDelayMs = 1_000
        resolve()
      })
      socket.addEventListener('message', event => {
        this.handleMessage(event.data)
      })
      socket.addEventListener('error', event => {
        this.emit('error', event)
        reject(new Error('Slack Socket Mode WebSocket error before open'))
      })
      socket.addEventListener('close', event => {
        this.emit('close', event)
        this.scheduleReconnect()
      })
    })
  }

  private handleMessage(data: unknown): void {
    const envelope = parseRawSocketEnvelope(data)
    if (!envelope) {
      logWarn('slack_socket_mode_invalid_websocket_message', {
        data_type: typeof data
      })
      return
    }

    if (envelope.type === 'hello') {
      logInfo('slack_socket_mode_connected')
      return
    }

    if (envelope.payload === undefined) {
      logWarn('slack_socket_mode_unsupported_websocket_envelope', {
        socket_type: envelope.type ?? null,
        has_envelope_id: Boolean(envelope.envelope_id)
      })
      return
    }

    const payload = envelope.payload as { type?: unknown; event?: { type?: unknown } }
    logInfo('slack_socket_mode_event_received', {
      socket_type: envelope.type ?? null,
      payload_type: typeof payload.type === 'string' ? payload.type : null,
      payload_event_type: typeof payload.event?.type === 'string' ? payload.event.type : null,
      has_envelope_id: Boolean(envelope.envelope_id)
    })

    this.emit('slack_event', {
      body: envelope.payload,
      ack: async () => {
        if (!envelope.envelope_id) return
        this.socket?.send(JSON.stringify({ envelope_id: envelope.envelope_id }))
      }
    })
  }

  private scheduleReconnect(): void {
    if (this.closed || this.reconnectTimer) return
    const delay = this.reconnectDelayMs
    this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, 30_000)
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect().catch(error => {
        this.emit('error', error)
        this.scheduleReconnect()
      })
    }, delay)
  }

  private emit(eventName: string, event: unknown): void {
    for (const listener of this.listeners.get(eventName) ?? []) {
      try {
        void listener(event)
      } catch (error) {
        logError('slack_socket_mode_listener_failed', error)
      }
    }
  }
}

async function openSocketModeUrl(opts: {
  appToken: string
  slackApiUrl?: string
}): Promise<string> {
  const apiBase = (opts.slackApiUrl ?? 'https://slack.com/api').replace(/\/$/, '')
  const response = await fetch(`${apiBase}/apps.connections.open`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${opts.appToken}`
    }
  })
  if (!response.ok) {
    throw new Error(`Slack apps.connections.open returned HTTP ${response.status}`)
  }

  const body = (await response.json()) as AppsConnectionsOpenResponse
  if (!body.ok || typeof body.url !== 'string') {
    throw new Error(`Slack apps.connections.open failed: ${body.error ?? 'missing_url'}`)
  }
  return body.url
}

function parseRawSocketEnvelope(data: unknown): RawSocketEnvelope | null {
  try {
    const text = typeof data === 'string' ? data : String(data)
    const parsed = JSON.parse(text) as RawSocketEnvelope
    if (!parsed || typeof parsed !== 'object') return null
    return parsed
  } catch {
    return null
  }
}
