# Koda Development Guide

## Branch structure

| Branch | Purpose |
|--------|---------|
| `main` | Stable. What `koda update` pulls for all users. Only merge here when tested. |
| `dev`  | Active development. Build and test here first. |

## The rule

**Never push directly to `main`.** All changes go to `dev` first, get tested locally, then merge to `main` when solid.

---

## Daily workflow

### Making changes

```bash
cd ~/projects/koda-agent
git checkout dev
# make your changes
koda                    # test locally — editable install reflects branch instantly
```

### Shipping to users

Once tested and working:

```bash
cd ~/projects/koda-agent
git checkout main
git merge dev
git push                # users get it on next koda update
git checkout dev        # back to dev for the next thing
```

### One-liner ship command

```bash
cd ~/projects/koda-agent && git checkout main && git merge dev && git push && git checkout dev
```

---

## For AI agents (Codex, Antigravity, Claude Code)

When making changes to this repo:
1. **Always work on `dev`** — never commit to `main`
2. The editable install is at `~/.koda/venv` pointing to `~/projects/koda-agent`
3. After pushing to `dev`, tell Jordan to test with `koda` then ship with the one-liner above
4. If Jordan says "push it" or "ship it" — that means merge dev → main and push

### Codex pattern

```bash
cd ~/projects/koda-agent && git checkout dev
# ... make changes ...
git add <files> && git commit -m "feat/fix: description" && git push
```

---

## Testing locally

Since the package is installed as editable (`pip install -e`), switching branches is instant:

```bash
git checkout dev    # koda now runs dev code
git checkout main   # koda now runs stable code
```

No reinstall needed unless you add new dependencies to `pyproject.toml`.

If you add new deps:
```bash
~/.koda/venv/bin/pip install -e ~/projects/koda-agent
```

---

## User-facing update command

Users run:
```bash
koda update    # pulls latest main, reinstalls if needed
```

This only pulls `main`. Users never see `dev` unless you merge.

---

## Config and data locations

All user data lives in `~/.koda/` — never in the repo:

| Path | Contents |
|------|---------|
| `~/.koda/.env` | API keys, user config |
| `~/.koda/Soul.md` | Koda's personality |
| `~/.koda/UserProfile.md` | User preferences |
| `~/.koda/koda_crons.json` | Scheduled jobs |
| `~/.koda/koda_skills.json` | Custom slash commands |
| `~/.koda/koda_cci.json` | CCI progress |
| `~/.koda/koda.log` | Session logs |
