# OddProofLab-Runner

Public execution-only repository for OddProofLab.

This repository contains only non-secret execution code, approved topic manifests, and public production state copied from the private `olkimol/OddProofLab` source of truth.

Purpose: run orchestration on GitHub-hosted runners without consuming the exhausted private-repository Actions budget.

Required encrypted repository secret:

- `KAGGLE_API_TOKEN` — Kaggle API token for account `olkimol`.

YouTube OAuth secrets are not stored here yet. Rendering and technical QA are automated; publication remains blocked until semantic visual/audio QA passes.
