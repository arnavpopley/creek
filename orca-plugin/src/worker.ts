// Creek's worker: finds the focused workspace's folder, asks the `creek` CLI for its
// sub-chats, and runs a sub-chat turn when the panel sends one. Creek keeps all state in
// the project's .creek/ ledger; this worker only relays.
import type { OrcaWorkerApi } from 'orca/worker'
import { execFile, spawn } from 'node:child_process'
import { existsSync, watch, type FSWatcher } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import './protocol'
import type { Subs } from './protocol'

function creekBin(): string {
  const fromEnv = process.env.CREEK_BIN
  if (fromEnv && existsSync(fromEnv)) return fromEnv
  for (const p of [join(homedir(), '.local/bin/creek'), '/opt/homebrew/bin/creek', '/usr/local/bin/creek']) {
    if (existsSync(p)) return p
  }
  return 'creek'
}

/** PATH for children: Orca may start workers without the login shell's PATH. */
function childEnv(): NodeJS.ProcessEnv {
  const extra = [join(homedir(), '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin']
  return { ...process.env, PATH: [...extra, process.env.PATH ?? ''].join(':') }
}

export async function focusedRoot(orca: OrcaWorkerApi): Promise<string | null> {
  const ctx = await orca.host.call('workspace.readContext', {})
  if (!ctx) return null
  const { workspaces } = await orca.host.call('workspace.list', {})
  const match =
    workspaces.find((w) => w.name === ctx.displayName && (w.branch ?? '') === (ctx.branch ?? '')) ??
    workspaces.find((w) => w.name === ctx.displayName) ??
    workspaces.find((w) => w.branch && w.branch === ctx.branch)
  return match?.path ?? null
}

function runList(root: string): Promise<Subs> {
  return new Promise((resolve, reject) => {
    execFile(creekBin(), ['sub', 'list', '--root', root], { env: childEnv(), timeout: 15_000 }, (err, stdout, stderr) => {
      if (err) return reject(new Error((stderr || err.message).trim()))
      try {
        resolve(JSON.parse(stdout) as Subs)
      } catch {
        reject(new Error('creek sub list gave no JSON'))
      }
    })
  })
}

let watcher: FSWatcher | null = null
let watchedRoot: string | null = null
let timer: ReturnType<typeof setTimeout> | null = null

/** Orca awaits this before stopping the worker: let go of the ledger watch. */
export function deactivate(): void {
  watcher?.close()
  watcher = null
  watchedRoot = null
  if (timer) clearTimeout(timer)
  timer = null
}

export default function activate(orca: OrcaWorkerApi): void {

  function watchLedger(root: string): void {
    if (watchedRoot === root) return
    watcher?.close()
    watcher = null
    watchedRoot = root
    const dir = join(root, '.creek')
    if (!existsSync(dir)) return
    try {
      // Any change to the ledger (the main chat or a sub-chat moved) refreshes open panels.
      watcher = watch(dir, (_event, file) => {
        if (file !== 'events.jsonl') return
        if (timer) clearTimeout(timer)
        timer = setTimeout(() => orca.panels.post('subs.changed', { reason: 'ledger' }), 300)
      })
    } catch (e) {
      orca.log(`creek: could not watch ${dir}: ${String(e)}`)
    }
  }

  orca.panels.handle('subs.get', async () => {
    const root = await focusedRoot(orca)
    if (!root) return { data: null, error: 'Open a workspace to see its sub-chats.' }
    watchLedger(root)
    try {
      return { data: await runList(root) }
    } catch (e) {
      return { data: null, error: `Creek isn't answering here: ${(e as Error).message}` }
    }
  })

  orca.panels.handle('subs.send', async ({ node, text }) => {
    const root = await focusedRoot(orca)
    if (!root) return { started: false, error: 'No workspace is focused.' }
    if (!text.trim()) return { started: false, error: 'Type a message first.' }
    const child = spawn(creekBin(), ['sub', 'send', node, text, '--root', root], {
      env: childEnv(),
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let out = ''
    child.stdout.on('data', (b) => (out += String(b)))
    child.stderr.on('data', (b) => orca.log(`creek sub: ${String(b).trim()}`))
    child.on('close', () => {
      let error: string | undefined
      try {
        const res = JSON.parse(out) as { ok: boolean; error?: string | null }
        if (!res.ok) error = res.error ?? 'failed'
      } catch {
        error = 'creek sub send gave no answer'
      }
      orca.panels.post('subs.changed', { reason: 'reply', node, error })
    })
    child.on('error', (e) => orca.panels.post('subs.changed', { reason: 'reply', node, error: e.message }))
    return { started: true }
  })

}
