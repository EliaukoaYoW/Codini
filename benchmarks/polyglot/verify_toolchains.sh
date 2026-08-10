#!/usr/bin/env bash
set -euo pipefail

# 功能：验证 Codini、Python 和 JavaScript 工具链；输入：无；输出：逐行版本信息并以进程状态表示可用性。
echo "codini=$(command -v codini)"
echo "javascript=$(node --version)"
echo "npm=$(npm --version)"
echo "jest=$(node -p "require('/opt/polyglot-js/node_modules/jest/package.json').version")"
echo "python=$(python3 --version)"
echo "pytest=$(pytest --version)"
