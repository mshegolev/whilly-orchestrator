#!/bin/bash
# Установка pre-commit хуков для Whilly

set -e

echo "🔧 Setting up Git hooks for Whilly..."

# Определяем корень репозитория
if [ -d ".git" ]; then
    REPO_ROOT="."
else
    REPO_ROOT=$(git rev-parse --show-toplevel)
fi

HOOKS_DIR="$REPO_ROOT/.git/hooks"
SOURCE_HOOK="$REPO_ROOT/.github/hooks/pre-commit"
TARGET_HOOK="$HOOKS_DIR/pre-commit"

# Проверяем что исходный хук существует
if [ ! -f "$SOURCE_HOOK" ]; then
    echo "❌ Source hook not found: $SOURCE_HOOK"
    exit 1
fi

# Создаем директорию хуков если нет
mkdir -p "$HOOKS_DIR"

# Копируем хук
echo "📋 Installing pre-commit hook..."
cp "$SOURCE_HOOK" "$TARGET_HOOK"
chmod +x "$TARGET_HOOK"

# Проверяем что хук установился
if [ -x "$TARGET_HOOK" ]; then
    echo "✅ Pre-commit hook installed successfully!"
    echo ""
    echo "🔍 Hook will check:"
    echo "  • YAML syntax (GitHub workflows)"
    echo "  • Python code quality (ruff)"
    echo "  • Potential secrets/tokens"
    echo "  • Large files (>10MB)"
    echo "  • GitHub Actions workflow structure"
    echo ""
    echo "💡 To bypass hook (emergency only): git commit --no-verify"
    echo ""

    # Тестируем хук
    echo "🧪 Testing hook installation..."
    if "$TARGET_HOOK" >/dev/null 2>&1; then
        echo "✅ Hook test passed!"
    else
        echo "⚠️  Hook test failed, but hook is installed"
    fi

else
    echo "❌ Failed to install pre-commit hook"
    exit 1
fi

# Опционально: настраиваем дополнительные инструменты
echo ""
echo "🛠️  Optional improvements:"

if ! command -v yamllint >/dev/null 2>&1; then
    echo "  • Install yamllint for better YAML validation:"
    echo "    pip install yamllint"
fi

if ! python3 -m ruff --version >/dev/null 2>&1; then
    echo "  • Install ruff for Python code checking (pinned in pyproject.toml):"
    echo "    pip install -e '.[dev]'"
fi

echo ""
echo "🎉 Git hooks setup complete!"
echo "Your commits are now protected against common issues! 🛡️"