#!/bin/bash
# Workshop Demo: Self-Writing Orchestrator with External Integrations
# Демонстрирует полный цикл: GitHub Issues → выполнение → автозакрытие

set -e

echo "🤖 WHILLY WORKSHOP: Self-Writing Orchestrator + External Integrations"
echo "======================================================================"
echo

# Цвета для вывода
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
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

function highlight() {
    echo -e "${CYAN}🔗 $1${NC}"
}

# Проверяем что мы в правильной директории
if [[ ! -f "whilly/cli.py" ]]; then
    error "Run this script from whilly-orchestrator root directory"
    exit 1
fi

step "1" "Checking GitHub CLI authorization"
unset GITHUB_TOKEN
if ! gh auth status > /dev/null 2>&1; then
    error "GitHub CLI not authorized. Please run: gh auth login"
    exit 1
fi
success "GitHub CLI авторизован"
echo

step "2" "Testing external integrations"
echo "Running integration test..."
python3 test_integrations.py
echo

step "3" "Setting up integration environment"
export WHILLY_CLOSE_EXTERNAL_TASKS=true
export WHILLY_GITHUB_AUTO_CLOSE=true
export WHILLY_GITHUB_ADD_COMMENTS=true

info "Environment configured:"
echo "  WHILLY_CLOSE_EXTERNAL_TASKS=true"
echo "  WHILLY_GITHUB_AUTO_CLOSE=true"
echo "  WHILLY_GITHUB_ADD_COMMENTS=true"
echo

step "4" "Showing current GitHub Issues"
echo "Current workshop Issues:"
gh issue list --label workshop --label whilly:ready | head -5
echo

step "5" "Converting GitHub Issues to Whilly tasks with integrations"
echo "This will preserve GitHub Issue links for auto-closing..."
python3 -m whilly --from-github workshop,whilly:ready || {
    error "Failed to convert GitHub issues"
    exit 1
}
success "GitHub Issues converted with integration metadata!"
echo

step "6" "Verifying integration fields in tasks"
if [[ -f "tasks-from-github.json" ]]; then
    echo "Sample task with integration fields:"
    jq '.tasks[0] | {id, description, github_issue, github_url}' tasks-from-github.json
    echo
    highlight "Integration fields preserved - ready for auto-closing!"
else
    error "tasks-from-github.json not found"
    exit 1
fi
echo

step "7" "Demo: What will happen when Whilly runs"
info "Whilly will:"
echo "1. 🔧 Execute each task (create code, run tests)"
echo "2. 💾 Create git commits with changes"
echo "3. 🔗 Add completion comment to GitHub Issue"
echo "4. ❌ Close GitHub Issue with reason 'completed'"
echo "5. ✅ Mark Whilly task as done"
echo

step "8" "Ready to run self-writing orchestrator with auto-close"
read -p "Start Whilly orchestrator with GitHub integration? [y/N]: " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${BLUE}🚀 Launching Whilly with external integrations enabled...${NC}"
    echo
    highlight "Watch the logs for integration messages:"
    echo "  '🔗 Closing external task: GITHUB #N'"
    echo "  '✅ Closed GITHUB #N successfully'"
    echo

    # Запускаем с интеграциями
    WHILLY_CLOSE_EXTERNAL_TASKS=true \
    WHILLY_GITHUB_AUTO_CLOSE=true \
    WHILLY_GITHUB_ADD_COMMENTS=true \
    python3 -m whilly tasks-from-github.json

    echo
    step "9" "Checking results"
    echo "Let's see which Issues were closed:"
    gh issue list --label workshop --state closed | head -5 || echo "No recently closed issues"

else
    info "Manual run command:"
    echo "  WHILLY_CLOSE_EXTERNAL_TASKS=true \\"
    echo "  WHILLY_GITHUB_AUTO_CLOSE=true \\"
    echo "  WHILLY_GITHUB_ADD_COMMENTS=true \\"
    echo "  python3 -m whilly tasks-from-github.json"
    echo
    info "Or test without closing:"
    echo "  WHILLY_CLOSE_EXTERNAL_TASKS=false python3 -m whilly tasks-from-github.json"
fi

echo
success "Workshop with external integrations complete! 🎉"
echo
highlight "Full automation achieved:"
echo "  GitHub Issues → Whilly Tasks → Code Changes → GitHub Comments → Closed Issues"
echo
info "Check GitHub Issues to see auto-generated completion comments!"
echo "Check git log to see auto-generated commits!"