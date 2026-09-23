# Flight Recorder to BlackBox provenance

> Historical migration record. This document is retained for repository provenance and is not part of the current BlackBox architecture or compatibility contract.

This repository was initialized from the Flight Recorder subtree in EVECOR and
then continued as BlackBox. The publication boundary is software-only: tracked
Flight Recorder `storage/` material is operational evidence/history and is not
part of published BlackBox history.

## Source and preserved extraction

- Source repository: `/mnt/jarvis-data/projects/EVECOR`
- Source EVECOR commit: `d62c5cd7fe4f5fc3f561f382a2908bcd2c7e6b65`
- Source subtree: `governance/flight-recorder`
- Exact extracted Flight Recorder commit: `e6d71a211422ea222d6cbc27cb562ca7a8e9812b`
- Exact extracted tree: `879a98172891776a53d7a4677ad149de2feb61f6`
- Donor subtree tree at source commit: `879a98172891776a53d7a4677ad149de2feb61f6`
- Private extraction clone:
  `/mnt/jarvis-data/migration-backups/blackbox/20260922T070643Z/extraction`
- Private bundle:
  `/mnt/jarvis-data/migration-backups/blackbox/20260922T070643Z/exact-flight-recorder-and-local-work.bundle`
- Private bundle SHA-256:
  `490e9d917bd81d6c1690eaa4e043427e8531b79980b7ade38cb773126d6f9607`
- Preserved Ledger working-state patch:
  `/mnt/jarvis-data/migration-backups/blackbox/20260922T070643Z/flight-recorder-ledger-working.patch`
- Preserved Ledger working-state patch SHA-256:
  `f9bf68e44481088ddbb2b994a430379e8ba37df25c46cabb0514f736af04a91a`

## Excluded operational paths

Reason for every exclusion: operational evidence/history, not BlackBox software
source. These files remain in the private exact extraction and bundle; they do
not enter BlackBox GitHub history.

| Path | SHA-256 |
| --- | --- |
| `storage/FLIGHT-RECORDER-TARGET.md` | `c16f5b651c3f52b6f1d9b07821d3a4c774255a8916bfb744035ac57bcedb1de2` |
| `storage/PROVENANCE_AND_FINDINGS.md` | `8d774fa0ca9415c403ff99c2895728815366c58c81f46ce39733a631cba3f6a4` |
| `storage/evidence/REPOWISE-LANCEDB-COMPAT-2026-08-09.md` | `cbc17113b3b2947b28ea41c15bd5ddde6baa73f0efe0f1e0f44bc9a2b555c4b7` |
| `storage/evidence/RESEARCH-AUDIT-2026-08-09.md` | `1f89e0dd14384b85b78b6ddf7cc0b364306a44ccd90d304ef44e4de7a3eb01c9` |
| `storage/evidence/phase01b-freeze.md` | `97e0ec628e7788b76ea6ab3fe2f14d7b0d78415d1eea295b7fd7f8933d039f91` |

## Published baseline

`v0.0.0-original` means the original publishable Flight Recorder software
baseline after `storage/` exclusions and before BlackBox rebranding, SQLite
redesign, or behavioral changes.

- Published baseline tag target: `4df10531d0d99382d1fc7b865aeea1aa74cdec88`
- Published baseline tree: `d44ac4e15381225aa407b271345e2d8d3360e5a3`
- Published baseline storage check: no paths under `storage/`

## Donor to published history mapping

| EVECOR donor commit | Exact extracted commit | Filtered BlackBox commit |
| --- | --- | --- |
| `c0269d76899068ed0fe6d7aef1cd3003054a9043` | `d170e82bd513626b3c1fa4e2dd2e2989e80ed8b6` | `26a263f5cae541e0900c7de9273d1f09f01317fa` |
| `f059d60a19dc2a702d8ccaafb97366645bbbfdae` | `f9fc2d96b9dfa4e5940c81e8cbbba1f44c394020` | `50929516eee4597031cb9023e4bd624d5a0b09bf` |
| `30fcc8a8f786026041820096160e761e1907b088` | `e09b23bfa57ab47975d12fc1f244a3bd087d664e` | `f7cdc71956c0a7acedfc5139151b7d330663493b` |
| `6cf3e1f0c77e56bab372d640dddc8ca1ef2350c7` | `26b794b55706a6c188c6d8004cb452d05859942b` | `fe126e0d7a5cfb241a387df04f70bc9e60fef5bd` |
| `3dac24ea8e5462a3470c86d6997d6848b6c02be7` | `e6d71a211422ea222d6cbc27cb562ca7a8e9812b` | `4df10531d0d99382d1fc7b865aeea1aa74cdec88` |

The imported BlackBox branch then merges the filtered baseline with the original
GitHub bootstrap commit `efea85ed04a6720e14ad7a7319742a87f6866294`, preserving
the bootstrap as the first GitHub repository commit.
