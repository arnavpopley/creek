// Runs the worker over Orca's mock host, with the real `creek` CLI on a demo project.
import { assert, mockHost, mountWorker, test } from 'orca/testing'
import * as main from './worker'

const ROOT = process.env.CREEK_TEST_ROOT ?? ''

test('lists the focused workspace sub-chats through creek', async () => {
  if (!ROOT) return
  const host = mockHost({
    results: {
      'workspace.readContext': { branch: 'main', displayName: 'demo', terminals: [] },
      'workspace.list': { workspaces: [{ id: 'w1', kind: 'folder', name: 'demo', path: ROOT, branch: 'main', hostId: null }], truncated: false }
    }
  })
  const worker = await mountWorker(main, { host })
  const res = (await worker.request('subs.get')) as { data: { subs: unknown[]; done: { node: string }[] } | null; error?: string }
  assert.equal(res.error, undefined)
  assert.ok(res.data)
  assert.deepEqual(res.data!.done.map((s) => s.node), ['n1', 'n2', 'n3'])
  assert.deepEqual(worker.refusals, [])
  await worker.deactivate()
})

test('no focused workspace', async () => {
  const host = mockHost({ results: { 'workspace.readContext': null } })
  const worker = await mountWorker(main, { host })
  const res = (await worker.request('subs.get')) as { data: unknown; error?: string }
  assert.equal(res.data, null)
  assert.ok(res.error)
  await worker.deactivate()
})
