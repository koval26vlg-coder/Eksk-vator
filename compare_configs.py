#!/usr/bin/env python3
"""Сравнение конфигураций и переключение между ними.

Usage:
    python compare_configs.py                    # Показать различия
    python compare_configs.py --use minimal      # Переключиться на minimal
    python compare_configs.py --use swing        # Переключиться на swing (текущий .env)
"""

import os
import sys
from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """Загрузить .env файл в словарь."""
    config = {}
    if not path.exists():
        return config

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Пропускаем комментарии и пустые строки
            if not line or line.startswith('#'):
                continue
            # Парсим KEY=VALUE
            if '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip()

    return config


def compare_configs():
    """Сравнить .env (swing) и .env.minimal."""
    root = Path(__file__).parent
    env_swing = load_env_file(root / '.env')
    env_minimal = load_env_file(root / '.env.minimal')

    print("=" * 80)
    print("СРАВНЕНИЕ КОНФИГУРАЦИЙ")
    print("=" * 80)
    print()

    # Ключевые параметры для сравнения
    key_params = [
        'BOT_STRATEGY',
        'SCALPING_VARIANT',
        'TA_TIMEFRAME',
        'AUTO_TRADE_NOTIONAL',
        'AUTO_TRADE_TP_BPS',
        'AUTO_TRADE_SL_BPS',
        'AUTO_TRADE_SL_ATR_MULT',
        'AUTO_TRADE_MAX_HOLD_SECONDS',
        'AUTO_TRADE_MAX_HOLD_HARD_SECONDS',
        'AUTO_TRADE_MAX_OPEN_ORDERS',
        'RISK_MAX_TOTAL_NOTIONAL',
        'CAPITAL_MAX_SESSION_LOSS_QUOTE',
        'TA_SMA_PERIOD',
        'TA_ADX_PERIOD',
        'TA_TREND_ADX_TRIGGER',
        'TA_RSI_OVERSOLD',
        'TA_RSI_OVERBOUGHT',
        'TA_TREND_MIN_DI_DIFF',
        'TA_TREND_MAX_RSI_LONG',
        'TA_TREND_MAX_EXTEND_BPS_LONG',
        'SCALPING_AUTO_TRADE_MIN_IMPULSE_BPS',
        'SCALPING_MIN_MID_RANGE_BPS',
        'AUTO_TRADE_EARLY_STOP_SECONDS',
        'AUTO_TRADE_MID_STOP_START_SECONDS',
        'AUTO_TRADE_AUTO_TUNE',
        'AUTO_TRADE_PROFILE',
        'SCALPING_AUTO_TRADE_VOL_SCALE',
        'SCALPING_SIGMA_SPIKE_ABS_BPS',
        'AUTO_TRADE_ATR_MAX_SPREAD_MULT',
    ]

    print(f"{'Параметр':<40} {'Swing (.env)':<20} {'Minimal':<20}")
    print("-" * 80)

    total_params_swing = len(env_swing)
    total_params_minimal = len(env_minimal)
    differences = 0

    for param in key_params:
        val_swing = env_swing.get(param, '—')
        val_minimal = env_minimal.get(param, '—')

        # Маркер различия
        marker = '  '
        if val_swing != val_minimal:
            marker = '⚠️'
            differences += 1

        print(f"{marker} {param:<38} {val_swing:<20} {val_minimal:<20}")

    print("-" * 80)
    print(f"\nВсего параметров:")
    print(f"  Swing (.env):     {total_params_swing}")
    print(f"  Minimal:          {total_params_minimal}")
    print(f"  Различий:         {differences}")
    print()

    print("=" * 80)
    print("КЛЮЧЕВЫЕ РАЗЛИЧИЯ")
    print("=" * 80)
    print()

    print("SWING (.env) — текущая конфигурация:")
    print("  ✓ 100+ параметров")
    print("  ✓ Множество фильтров (RSI, DI, impulse, range, ATR, sigma)")
    print("  ✓ Сложные выходы (EARLY_STOP, MID_STOP, MIN_PNL)")
    print("  ✓ AUTO_TUNE, VOL_SCALE, SIGMA_SPIKE")
    print("  ⚠️ Риск overfitting: нужно 3,000+ сделок для валидации")
    print()

    print("MINIMAL (.env.minimal) — упрощённая:")
    print("  ✓ 8 ключевых параметров")
    print("  ✓ Только SMA + ADX для входа")
    print("  ✓ Только TP/SL для выхода")
    print("  ✓ Нет фильтров (чистая идея)")
    print("  ✓ Нужно 240 сделок для валидации (2 месяца swing)")
    print()

    print("=" * 80)
    print("РЕКОМЕНДАЦИЯ")
    print("=" * 80)
    print()
    print("1. Начните с MINIMAL (2 месяца paper test)")
    print("2. Если profit factor > 1.5 → добавьте 1-2 фильтра")
    print("3. Если profit factor < 1.2 → проблема в идее, не в параметрах")
    print()
    print("Переключиться:")
    print("  python compare_configs.py --use minimal")
    print("  python compare_configs.py --use swing")
    print()


def switch_config(target: str):
    """Переключиться на указанную конфигурацию."""
    root = Path(__file__).parent
    env_path = root / '.env'
    backup_path = root / '.env.backup'

    if target == 'minimal':
        minimal_path = root / '.env.minimal'
        if not minimal_path.exists():
            print(f"❌ Файл {minimal_path} не найден")
            sys.exit(1)

        # Бэкап текущего .env
        if env_path.exists():
            import shutil
            shutil.copy(env_path, backup_path)
            print(f"✓ Бэкап создан: {backup_path}")

        # Копируем minimal → .env
        import shutil
        shutil.copy(minimal_path, env_path)
        print(f"✓ Переключено на MINIMAL конфигурацию")
        print(f"  Параметров: 8")
        print(f"  Стратегия: SMA + ADX (trend following)")
        print(f"  Фильтры: отключены")
        print()
        print("Запуск:")
        print("  python main.py")

    elif target == 'swing':
        if not backup_path.exists():
            print(f"❌ Бэкап {backup_path} не найден")
            print("   Текущий .env уже является swing конфигурацией")
            sys.exit(1)

        # Восстанавливаем из бэкапа
        import shutil
        shutil.copy(backup_path, env_path)
        print(f"✓ Восстановлена SWING конфигурация из бэкапа")
        print(f"  Параметров: 100+")
        print(f"  Стратегия: ta_trend с множеством фильтров")
        print()
        print("Запуск:")
        print("  python main.py")

    else:
        print(f"❌ Неизвестная конфигурация: {target}")
        print("   Доступно: minimal, swing")
        sys.exit(1)


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == '--use' and len(sys.argv) > 2:
            switch_config(sys.argv[2])
        else:
            print("Usage:")
            print("  python compare_configs.py                # Показать различия")
            print("  python compare_configs.py --use minimal  # Переключиться на minimal")
            print("  python compare_configs.py --use swing    # Переключиться на swing")
            sys.exit(1)
    else:
        compare_configs()


if __name__ == '__main__':
    main()
