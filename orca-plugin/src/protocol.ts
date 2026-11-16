// The panel <-> worker contract.
export {}

export type SubMessage = { who: 'you' | 'claude'; text: string; ts?: string }
export type Sub = {
  node: string
  num: number
  title: string
  status: 'open' | 'resolved' | 'tabled' | 'abandoned'
  busy: boolean
  thread: SubMessage[]
  waiting: string | null
  latest: string | null
}
export type Subs = {
  project: string
  root: string
  main_session: string | null
  brief: string | null
  subs: Sub[]
  done: Sub[]
}

declare module 'orca' {
  interface PluginRequests {
    /** The sub-chats of the focused workspace, or why there are none. */
    'subs.get': { input: { root?: string | null }; output: { data: Subs | null; projects: { path: string; label: string }[]; error?: string } }
    /** Starts one turn in a point's sub-chat; the answer arrives as subs.changed. */
    'subs.send': { input: { node: string; text: string; root: string }; output: { started: boolean; error?: string } }
  }
  interface PluginPanelEvents {
    /** The ledger changed (a sub-chat replied, or the main chat moved on). */
    'subs.changed': { reason: string; node?: string; error?: string }
  }
}
