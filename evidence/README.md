# evidence/

Real output from the shipped code, committed so a reviewer does not have to take
a claim on trust.

| file | produced by | what it shows |
|---|---|---|
| `jmp-cvm-probe.json` | `python3 scripts/jmp_cvm_probe.py --out evidence/jmp-cvm-probe.json` | what the served JMP tools answer today: `jmp.status` returning the capture with `source: cached`, `jmp.funding` refusing with `rail_unavailable` (no live driver wired), and `jmp.credentials` refusing with `owner_only` plus the store precondition report (`store: openbao`, `present: false`) — the documented OpenBao blocker, from a real run. |

Nothing in this directory is hand-written: re-run the command above and the file
should be byte-identical apart from the timestamps the tool returns.
