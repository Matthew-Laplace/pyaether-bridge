# DeepSeek Harness

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`)
composes a profile from plugin bundles. This bridge is a plain MCP server, so it
is registered **by a bundle**: a small local package whose `cordis.patch.yml`
inserts one `@deepseek-ai/dsh-mcp-client` row. That bundle is what makes the
tools appear as `mcp__pyaether__*`.

## Two halves

| Half | What it is | Who installs it |
| --- | --- | --- |
| Registration (`mcp__pyaether__*`) | the bundle `dsh-bundle-pyaether-bridge`, kept next to this repository | the harness' own plugin manager |
| Skill (how to drive the tools) | `<workspace>/.dsh/skills/pyaether-bridge/SKILL.md` | `pyaether dsh install` |

## Why registration is a bundle, not a written patch

Adding the row straight to a profile's `cordis.patch.yml` looks simpler, and does
work for a profile the `dsh` CLI owns. It does **not** work for a profile the
desktop application manages, which is the normal case on a workstation:

```
$ pyaether dsh install --profile desktop
error: the harness rejected the edited patch file, so the change was rolled back
(.../cordis.patch.yml). Harness output:
error: profile "desktop" is managed exclusively by the Electron application
```

The composition check can never pass there, so a careful installer rolls itself
back -- and an installer that always rolls back is worse than none. The bundle
channel sidesteps the question: a bundle is a separate package, so nothing edits
the profile by hand.

## Install

**1. The bundle.** Through the harness' plugin manager (or the Plugins page in the
GUI), pointing at the local directory:

```
link:/abs/path/to/<workspace>/dsh-bundle-pyaether-bridge
```

The bundle contributes exactly one row:

```yaml
- insert:
    - id: mcp-pyaether-bridge
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: pyaether
        transport: stdio
        command: '/usr/bin/python3'
        args: ['/abs/path/to/pyaether-bridge/bin/pyaether-mcp']
        toolCallTimeoutMs: 1800000
        failOnStartupError: false
        reconnect:
          enabled: true
```

**2. The skill**, into the workspace:

```bash
./bin/pyaether dsh install     # -> <workspace>/.dsh/skills/pyaether-bridge/SKILL.md
./bin/pyaether dsh status      # is it installed, and does a profile scan it?
./bin/pyaether dsh uninstall   # remove it again
```

`--workspace DIR` overrides the workspace root; `--dsh-home DIR` overrides
`$DSH_HOME` for the read-only reporting `status` does.

## Why the skill is workspace-level, and why `status` checks the root

The filesystem skill provider ships disabled. Once enabled it reads an explicit
`customSkillDirs` list, so the global `$DSH_HOME/skills` directory is never
scanned: a skill written there is installed and invisible. The skill therefore
goes under the workspace, and `status` reports whether a profile actually scans
that root -- installed-but-unscanned changes nothing:

```
skill      : /.../模拟开发/.dsh/skills/pyaether-bridge/SKILL.md
in sync    : yes
skill root : /.../模拟开发/.dsh/skills
scanned by : desktop
bundle     : /.../模拟开发/dsh-bundle-pyaether-bridge
registered : desktop -> mcp__pyaether__*
```

The workspace root is resolved the same way the harness' instruction loader
resolves it: the nearest ancestor carrying a `.dsh-workspace` marker (or
`--workspace` / `$DSH_WORKSPACE`), so a session started deep inside a project
still finds the same root.

To make a root loadable, list it in the provider row of the profile patch:

```yaml
- id: skill-filesystem
  name: "@deepseek-ai/dsh-skill-filesystem"
  disabled: false
  config:
    customSkillDirs:
      - "/abs/path/to/<workspace>/.dsh/skills"
```

`pyaether dsh install` prints exactly this block when no profile scans the root
yet, rather than reporting a success that would never load.

## After installing

Reload the profile (or restart the harness). The tools then appear as
`mcp__pyaether__*`. `pyaether dsh status` shows the wiring, but only the harness
can prove the tools are actually callable -- confirm the `mcp__pyaether__*` tools
are present before relying on them.

## Notes

- `toolCallTimeoutMs` is raised deliberately: the MCP client defaults to 60 s per
  call, and a simulation or a layout job legitimately runs for minutes.
- The command is absolute: the harness starts stdio servers with a scrubbed
  environment from its own working directory, so `PATH` lookup and relative paths
  are not reliable.
- Target settings (container / transport / interpreter / docs dir) are **not** in
  the bundle: the bridge resolves them from its own profile config
  (`~/.cache/pyaether-bridge/config.json`), so there is one source of truth.
- Nothing here writes into a profile. `tests/dsh_probe.py` asserts that in a
  throwaway workspace and `DSH_HOME`, and covers marker resolution,
  install / status / uninstall, and reporting of a disabled provider row.
