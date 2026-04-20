#!/bin/bash
# Workshop Demo: Self-Writing Orchestrator
# Демонстрирует как Whilly сам себя улучшает через GitHub Issues

set -e

echo "🎯 WHILLY WORKSHOP: Self-Writing Orchestrator"
echo "=============================================="
echo

# Цвета для вывода
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

function step() {
    echo -e "${BLUE}📋 Step $1: $2${NC}"
}

function success() {
    echo -e "${GREEN}✅ $1${NC}"
}

function info() {
    echo -e "${YELLOW}💡 $1${NC}"
}

function error() {
    echo -e "${RED}❌ $1${NC}"
}

# Проверяем что мы в правильной директории
if [[ ! -f "whilly/cli.py" ]]; then
    error "Run this script from whilly-orchestrator root directory"
    exit 1
fi

step "1" "Checking GitHub Issues"
# Clear problematic GITHUB_TOKEN
unset GITHUB_TOKEN
echo "Current workshop Issues:"
gh issue list --label workshop --label whilly:ready || {
    error "Failed to list GitHub issues. Make sure gh is authenticated."
    exit 1
}
echo

step "2" "Converting GitHub Issues to Whilly tasks"
python3 -m whilly --from-github workshop,whilly:ready || {
    error "Failed to convert GitHub issues"
    exit 1
}
success "GitHub Issues converted to tasks!"
echo

step "3" "Showing generated tasks"
if [[ -f "tasks-from-github.json" ]]; then
    echo "Generated tasks:"
    jq -r '.tasks[] | "- \(.id): \(.description)"' tasks-from-github.json
    echo
    success "$(jq '.tasks | length' tasks-from-github.json) tasks ready for execution"
else
    error "tasks-from-github.json not found"
    exit 1
fi
echo

info "Demo setup complete! Next steps:"
echo "1. Run: python3 -m whilly tasks-from-github.json"
echo "2. Watch Whilly automatically improve itself"
echo "3. Check resulting PRs on GitHub"
echo

step "4" "Optional: Start Whilly orchestrator now"
read -p "Start Whilly orchestrator automatically? [y/N]: " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${BLUE}🚀 Launching Whilly orchestrator...${NC}"
    echo
    python3 -m whilly tasks-from-github.json
else
    info "Run manually: python3 -m whilly tasks-from-github.json"
fi

success "Workshop demo complete! 🎉"