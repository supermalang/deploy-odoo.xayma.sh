#!/usr/bin/env bash
set -euo pipefail

echo "==> Installing Ansible toolchain"
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir ansible ansible-lint

echo "==> Installing Claude Code CLI"
npm install -g @anthropic-ai/claude-code

# Authenticate the GitHub CLI from the PAT if one was provided via secrets.env.
if [ -n "${GH_TOKEN:-}" ]; then
  echo "==> Configuring GitHub CLI auth"
  git config --global credential.helper '!gh auth git-credential'
fi

echo "==> Versions"
ansible --version | head -n 1
ansible-lint --version || true
gh --version | head -n 1

echo "==> Dev container ready"
