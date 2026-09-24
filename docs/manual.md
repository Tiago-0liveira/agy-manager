# agym User Manual

## Account Management Commands

### `smartrename`

Renames all available accounts in a clean, sequential order.

**Usage**:
```bash
agym smartrename <num|letter>
```

**Arguments**:
- `num`: Renames accounts to numeric identifiers starting at `1` up to `n` (e.g., `1`, `2`, `3`, ...).
- `letter`: Renames accounts to alphabetical identifiers:
  - Accounts 1–26: `A` to `Z`
  - Accounts 27–52: `Aa` to `Az`
  - Accounts 53–78: `Ba` to `Bz`
  - Accounts 703+: `Aaa`, `Aab`, ...

**Collision Safety**:
Renames are performed using a two-phase staging algorithm:
1. **Phase 1 (Staging)**: All accounts are first safely renamed to collision-free temporary identifiers (`tmp_sr_<uuid>_<index>`).
2. **Phase 2 (Commit)**: Staged accounts are renamed to their calculated target names.

If any failure occurs during either phase, accounts are restored to their original names.

**Examples**:
```bash
$ agym smartrename num
Renamed 3 accounts using 'num' sequence:
  [1/3] OldName1 -> 1
  [2/3] OldName2 -> 2
  [3/3] OldName3 -> 3

$ agym smartrename letter
Renamed 28 accounts using 'letter' sequence:
  [1/28] old1 -> A
  ...
  [26/28] old26 -> Z
  [27/28] old27 -> Aa
  [28/28] old28 -> Ab
```
