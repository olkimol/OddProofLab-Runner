# OddProofLab-Runner

Public execution-only repository for OddProofLab.

The private repository `olkimol/OddProofLab` remains the source of truth for approved topic manifests, renderer code, QA rules, publication registry, and YouTube OAuth credentials.

This repository exists only to run orchestration on GitHub-hosted runners without consuming the exhausted private-repository Actions budget.

Required encrypted repository secrets:

- `ODDPROOF_PRIVATE_REPO_TOKEN` — fine-grained GitHub token with read access only to `olkimol/OddProofLab`.
- `KAGGLE_API_TOKEN` — Kaggle API token for account `olkimol`.

YouTube OAuth secrets stay in the private repository and are not copied here.
