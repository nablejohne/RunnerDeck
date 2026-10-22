# Fixtures

Real output captured during the M0 spike on Windows (2026-10-22, runner 2.338.0, zh-CN console).
See docs/spike-notes.md for context.

- `stdout/*.txt`: run.cmd output, decoded, timing prefix stripped
- `stdout/*.cp936.raw`: the same capture as raw bytes; cmd.exe's own lines are GBK, runner lines ASCII
- `config/`: `config.cmd --unattended` and `config.cmd remove` output (tokens redacted at capture)
