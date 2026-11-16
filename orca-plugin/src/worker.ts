// Creek's worker: finds the focused workspace's folder, asks the `creek` CLI for its
// sub-chats, and runs a sub-chat turn when the panel sends one. Creek keeps all state in
// the project's .creek/ ledger; this worker only relays.
import type { OrcaWorkerApi } from 'orca/worker'
import { execFile, spawn } from 'node:child_process'
import { existsSync, readFileSync, statSync, watch, type FSWatcher } from 'node:fs'
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

type Place = { name: string; project: string | null; path: string; mtime: number; briefed: boolean }

const norm = (b: string | null | undefined) => (b ?? '').replace(/^refs\/heads\//, '')

/** A workspace's Creek ledger, if it has one: when it last changed and whether it holds a brief. */
function ledgerInfo(path: string): { mtime: number; briefed: boolean } | null {
  const file = join(path, '.creek', 'events.jsonl')
  try {
    const st = statSync(file)
    // Briefs are rare and small; the ledger is append-only text.
    const briefed = st.size > 0 && readFileSync(file, 'utf8').includes('"origin":"brief"')
    return { mtime: st.mtimeMs, briefed }
  } catch {
    return null
  }
}

/** Projects with a Creek ledger, the focused workspace's matches first, then the most recently active. */
export async function places(orca: OrcaWorkerApi): Promise<{ list: Place[]; focused: string | null }> {
  const ctx = await orca.host.call('workspace.readContext', {})
  const { workspaces } = await orca.host.call('workspace.list', {})
  const list: Place[] = []
  for (const w of workspaces) {
    const info = w.path ? ledgerInfo(w.path) : null
    if (info) list.push({ name: w.name, project: w.project ?? null, path: w.path, ...info })
  }
  list.sort((a, b) => Number(b.briefed) - Number(a.briefed) || b.mtime - a.mtime)
  let focused: string | null = null
  if (ctx) {
    // Orca gives only the focused workspace's name and branch; many are called "main".
    const same = list.filter((p) => p.name === ctx.displayName && norm(workspaces.find((w) => w.path === p.path)?.branch) === norm(ctx.branch))
    // Only trust the focus when it points at a project with a brief; names alone are ambiguous.
    focused = same.find((p) => p.briefed)?.path ?? null
  }
  return { list, focused }
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

  orca.panels.handle('subs.get', async (input) => {
    const { list, focused } = await places(orca)
    const wanted = input?.root && list.some((p) => p.path === input.root) ? input.root : null
    const root = wanted ?? focused ?? list.find((p) => p.briefed)?.path ?? null
    const projects = list.filter((p) => p.briefed).map((p) => ({ path: p.path, label: p.project ? `${p.project} · ${p.name}` : p.name }))
    if (!root) return { data: null, projects, error: 'Send a numbered list in a chat first; Creek makes its sub-chats from it.' }
    watchLedger(root)
    try {
      return { data: await runList(root), projects }
    } catch (e) {
      return { data: null, projects, error: `Creek isn't answering here: ${(e as Error).message}` }
    }
  })

  orca.panels.handle('subs.send', async ({ node, text, root }) => {
    if (!root || !existsSync(join(root, '.creek'))) return { started: false, error: 'No Creek project is selected.' }
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
