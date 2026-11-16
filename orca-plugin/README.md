# Creek for Orca

A sidebar panel that gives each point of your brief its own sub-chat, next to your Orca chat.

- Send a numbered list in the chat. Each point appears here as a branch with its own box.
- Type into a point's box and only that point moves: the message goes to a fork of your main
  Claude session (so it has all the context), and the reply lands in that sub-chat.
- When Claude settles a point, its sub-chat merges and dissolves away, and the main chat gets
  one line about it on your next message.

Sub-chats run with the same permission mode as the main chat. Needs the `creek` CLI
(`pipx install git+https://github.com/arnavpopley/creek`) and the Creek Claude Code plugin.

Why a sidebar and not boxes inside the chat: Orca draws in-chat pages with
`connect-src 'none'` and `form-action 'none'`, and only lets them report their size, scroll,
and open web links; nothing in Orca's CLI or plugin API can post a message into a chat tab.
A sidebar panel can talk to its own worker, which can run `creek`.
