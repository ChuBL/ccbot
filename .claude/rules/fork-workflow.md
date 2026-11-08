# Fork and PR workflow (this fork only, branch `patched`)

This file lives only on `patched`. Keep it out of `pr/*` branches.

## Repositories and branches

- `~/ccbot/main` on branch `patched` is the source of truth for what runs here. PR worktree: `~/ccbot/prs`.
- `upstream` is six-ddc/ccbot (formerly ccmux), push disabled.
- `origin` is the owner's fork `ChuBL/ccbot`, reached with the single-repo deploy key `~/.ssh/ccbot_fork_ed25519` (`core.sshCommand`, no gh login). The GitHub login is ChuBL, not gene21d4.
- Commits here use `30461992+ChuBL@users.noreply.github.com`, so the gmail address stays private.

## Rules

- Push to the fork without asking.
- PRs to six-ddc/ccbot are discussed one by one with the owner first, and the owner opens them on the web. Never open one yourself.
- `pr/*` branches on the fork are each based on upstream/main (only agent-picker stacks on agent-view), with draft descriptions in `~/.ccbot/pr-notes`. They conflict with each other, so each merge means rebasing the rest.
- Never pull upstream. `scripts/check-upstream.sh` only fetches and reports overlap and a trial merge; the weekly `ccbot-upstream-check.timer` runs it with `--notify`. That timer and script's notify setup are local, not for PRs.

## Deploy

```
uv pip install --python ~/.local/share/uv/tools/ccbot/bin/python --no-deps --reinstall ~/ccbot/main
systemctl --user restart ccbot
```

`uv tool upgrade` or `uv tool install` go back to the old pin and drop every local change. The service has KillMode=process, so restarting it leaves tmux alone.

## Tests

```
env -u TMUX_SESSION_NAME uv run pytest tests/ccbot -q
```

A shell with `TMUX_SESSION_NAME` set fails five session-map migration tests.
