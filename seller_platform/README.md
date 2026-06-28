# Seller Platform — desktop-скрипт упаковщика (форк)

Форк seller-support-скрипта (`D:\dev\SellerSuppScript`, мебельный цех) под продукт
**Seller Platform** (только маркетплейсы). Исходный скрипт **не трогается** — этот форк
живёт в подпапке `seller_platform/` и переиспользует общий `.venv` и бинарь печати.

## Что изменилось относительно seller-support-скрипта

| Было (seller-support) | Стало (seller-platform) |
|---|---|
| Хост хардкод `seller-supp.ru`, `verify=False` | `config.ini` рядом с .exe (`host`, `verify_ssl`) |
| Выбор enum-**рабочего места** (`GET /admin/users/{u}/workplaces`) | Выбор **этапа** (`GET /api/v1/stages/my-workstations`) |
| `work/process` шлёт `{username, workplace}` | `work/process` шлёт `{username, stageCode}` |
| 5 furniture-виджетов (Пила/Кромка/ЧПУ/Упаковка/Упаковка-мебель) | 1 универсальный `StageWidget` |
| Вторичная авторизация пар Пила-1/Пила-2 | убрана |
| Чекбокс «Фасад» (`isEmployeePreparedFacade`) | убран |
| Печать (PDFtoPrinter, 58×40, фокус-фикс, ding) | **перенесено 1:1** |

## Структура

```
seller_platform/
  main.py                      ← точка входа
  auth_gui.py                  ← логин → выбор этапа
  workstations_choice.py       ← выбор этапа (my-workstations)
  stage_widget.py              ← скан → валидация → печать → ЗП; «Брак» → PENALTY
  api.py                       ← HTTP-клиент + печать (форк seller_supp_api.py)
  config.ini                   ← host / verify_ssl (редактируется на месте)
  PDFtoPrinter.exe, ding.wav   ← ресурсы (копии из корня репо)
  seller-platform-packer.spec  ← сборка PyInstaller
```

## Запуск из исходников (dev)

```powershell
cd D:\dev\SellerSuppScript\seller_platform
..\.venv\Scripts\python.exe main.py
```

Поднятый бэкенд seller-platform на `http://localhost:8082` (см. `config.ini`).
Тестовая учётка: `admin` / `admin` (должна быть назначена на этап с `has_workstation_tab`,
напр. `UPAKOVKA`, иначе список этапов будет пуст).

## Сборка .exe

```powershell
cd D:\dev\SellerSuppScript\seller_platform
..\.venv\Scripts\pyinstaller.exe seller-platform-packer.spec
```

Результат: `dist\seller-platform-packer.exe`. Рядом с .exe положить `config.ini`
(прод-хост, `verify_ssl = true`).

## Поток упаковщика

1. Логин → выбор этапа (напр. «Упаковка»).
2. Скан QR / ввод номера заказа → Enter.
3. Валидация заказа → загрузка этикетки (`GET /orders/{n}/package`) → печать (58×40) →
   `POST /work/process` `{username, stageCode, EARNING}` (терминальный этап → списание склада МП на бэке).
4. Чекбокс «Брак» → `PENALTY` без печати.

## Открытые вопросы (см. вики-план)

- OQ-1: семантика `POST /orders/validation` для МП-заказа (сейчас шлём `{orderNumber}` без furniture-полей).
- OQ-3: generic-виджет пока рассчитан на терминальный этап (упаковка, печатает этикетку);
  нетерминальные этапы (печать не нужна) — отдельная доработка.
- Bulk `/orders/packages` намеренно не переносился (был для furniture-партий).
