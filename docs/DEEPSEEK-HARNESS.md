# DeepSeek Harness

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`)
composes a profile from plugin bundles. One of the plugins it ships is an MCP
client (`@deepseek-ai/dsh-mcp-client`) that registers an external MCP server's
tools on the agent's tool list. This bridge already speaks MCP, so using it as a
DSH plugin is one profile entry plus one skill -- no Node package of our own.

## Install

```bash
./bin/pyaether dsh status --profile desktop   # installed for this profile?
./bin/pyaether dsh patch --profile desktop    # print the patch to apply by hand
./bin/pyaether dsh install --profile desktop  # write it, plus the skill
./bin/pyaether dsh verify --profile desktop   # prove the harness composes it
```

`--profile` defaults to `web`, so **pass the profile you actually run**: `echo
$DSH_PROFILE` prints its name and `$DSH_PROFILE_DIR` its directory. Installing
into a profile the harness never loads looks successful and changes nothing. The
patch entry is per profile; the skill is written once, to
`<dsh-home>/skills/pyaether-bridge/SKILL.md`.

`--dsh-home` overrides `$DSH_HOME` (default `~/.dsh`); that is how the test suite
exercises this without touching your real configuration.

After installing, restart the harness or reload the profile. The tools then
appear as `mcp__pyaether__*`.

## What gets written

`install` appends one entry to `<dsh-home>/profiles/<profile>/cordis.patch.yml`
(keeping a timestamped backup) and copies the skill to
`<dsh-home>/skills/pyaether-bridge/SKILL.md`:

```yaml
- insert:
    - id: "pyaether-mcp"
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: "pyaether"
        transport: "stdio"
        command: "/usr/bin/python3"
        args: ["/abs/path/to/pyaether-bridge/bin/pyaether-mcp"]
        toolCallTimeoutMs: 1800000
        failOnStartupError: false
```

Four details are load-bearing, and each of them was a real failure first:

1. **`- insert:` is required.** A top-level `- id:` item is a *patch* aimed at an
   existing entry id, so an entry whose id does not exist yet is silently
   dropped: the file parses, the harness starts, and the plugin never appears.
2. **The scoped package name must be quoted.** `name: @deepseek-ai/...` is invalid
   YAML (`@` is a reserved indicator) and the harness refuses to boot the profile.
3. **`toolCallTimeoutMs` is raised deliberately.** The MCP client defaults to 60 s
   per call, but a simulation or a layout job may legitimately run for minutes,
   so the default turns long runs into timeouts.
4. **The command is absolute.** The harness starts stdio servers with a scrubbed
   environment from its own working directory, so `PATH` lookup and relative
   paths are not reliable.

## Lifecycle guarantees

* `install` never rewrites your patch file: it appends, after saving a backup.
  Running it twice does not duplicate the entry.
* An empty `[]` layer is handled correctly. Appending block items after `[]`
  produces invalid YAML, so that case writes a fresh list instead.
* A file the installer does not understand (an inline flow sequence, for
  example) is refused with the block to paste, rather than being corrupted.
* After editing, it asks the harness. If `dsh --dump-config` fails, the edit is
  rolled back so you are never left with a profile that cannot boot.
* `uninstall` removes the entry, drops the `insert` block when it becomes empty,
  leaves a valid empty list behind, and deletes the skill.

## Verifying

`./bin/pyaether dsh verify` runs the real `dsh --profile <p> --dump-config` and
checks that the composed tree contains the entry. A patch file on disk is not
evidence: it can be overridden by a later layer or ignored entirely.

`tests/dsh_probe.py` performs the whole cycle in a throwaway `DSH_HOME`: the
baseline composes, install writes the entry, the composed tree contains it, a
second install is idempotent, and uninstall leaves a valid profile.

## Notes

* Only the MCP client plugin is used. No third-party DSH plugin is installed and
  no build script runs outside the agent sandbox.
* Verified against DeepSeek Harness `0.2.0-rc.2`. DSH is a developer preview, so
  re-run `dsh verify` after upgrading it.
