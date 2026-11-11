# GitHub, VS Code и Codex для команды

## 1. Открыть общий репозиторий

На GitHub откройте репозиторий → **Settings** → **Collaborators** → **Add people**. Пригласите Мишу и Кирилла по их GitHub-логинам. В репозитории организации используйте **Collaborators & teams** и выберите право, достаточное для работы с ветками. После принятия приглашений каждый открывает репозиторий под своим аккаунтом. [GitHub Docs о приглашениях](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/inviting-collaborators-to-a-personal-repository)

Если на GitHub уже есть коммит, возьмите HTTPS-адрес через **Code** и клонируйте репозиторий в соседнюю папку. Перенесите туда подготовленные документы, включая `AGENTS.md` и `docs/`. Если в удалённом репозитории уже есть README, сохраните его содержание и добавьте ссылки на документы. [GitHub Docs о клонировании](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository)

```powershell
git clone https://github.com/OWNER/REPO.git energytool
Set-Location energytool
git remote -v
git status --short --branch
```

Если удалённый репозиторий пуст, можно продолжить в текущей локальной папке. В ней на момент подготовки есть локальный Git, ветка `master` без коммитов и отсутствует `origin`. Выполните из этой папки, подставив свой адрес. Если состояние с тех пор изменилось, сначала проверьте текущую ветку и remote.

```powershell
git remote -v
git remote add origin https://github.com/OWNER/REPO.git
git branch -m main
git status --short --branch
```

## 2. Запустить Codex в VS Code

1. В VS Code выберите **File → Open Folder** и откройте папку с `.git`, `AGENTS.md` и `docs/`.
2. Установите официальное расширение **Codex** от OpenAI (`openai.chatgpt)) и войдите в ChatGPT. Откройте панель по значку Codex в VS Code. Если значка не видно, откройте палитру через `F1` или `Ctrl+Shift+P` и введите `Codex`. В английском интерфейсе команда называется **Codex: Open Codex Sidebar**. Если расширение только что установили и команды пока нет, выполните **Developer: Reload Window** и повторите поиск. В панели **Extensions** по запросу `@installed openai.chatgpt` проверьте, что расширение включено для текущего профиля и папки. Работайте в локальном режиме для изменений в этой папке. [Документация OpenAI для IDE](https://developers.openai.com/codex/ide/)
3. В чате Codex укажите относительные пути к ТЗ и правилам. Codex загружает корневой `AGENTS.md` при запуске работы в проекте. Чтобы подхватить новые инструкции, начните новый чат после изменения файла. Вход в Codex используется для работы агента. Доступ GitHub к репозиторию и доступ Python к прокси хакатона настраиваются отдельно. [Документация OpenAI по AGENTS.md](https://developers.openai.com/codex/guides/agents-md/) · [Спецификация AGENTS.md](https://agents.md/)

Сообщение для первого запуска в VS Code:

> Открой AGENTS.md, docs/MVP-SPEC.md, docs/ARCHITECTURE.md и docs/TODAY-CHECKLIST.md. Создай фундамент этапа 1 по ТЗ. Согласуй интерфейсы функций и типы, реализуй сквозной запуск на синтетической базе со стабом прокси, оформи README. Подключи субагентов для проверки по §8–9. Покажи diff, команды, результаты и открытые проблемы. Git commit и git push только по моему прямому указанию.

Чтобы продолжить из Codex Desktop, откройте в нём тот же локальный каталог. Изменения видны в обоих редакторах через общие файлы. Для работы с локальным клоном отдельный GitHub-коннектор не требуется.

Если вверху видно **Restricted Mode**, нажмите **Manage**. Если доверяете содержимому, выберите **Trust** для папки «МТС хакатон». Затем нажмите `F1` и введите `Codex`. VS Code может скрывать команды и значки установленных расширений в Restricted Mode. [Документация VS Code о Workspace Trust](https://code.visualstudio.com/docs/editing/workspaces/workspace-trust)

## 3. Опубликовать фундамент

В первый набор изменений включите `AGENTS.md`, README, документы из `docs/`, `.gitignore`, контракты и точку входа, `requirements.txt` и синтетическую проверку запуска. В README зафиксируйте каталог входов, переменные прокси, запуск, тест и упаковку. В `.gitignore` внесите `data/`, локальные отчёты и архивы, `submission.csv`, `.venv/`, `__pycache__/` и файлы с секретами вроде `.env`. Материалы кейса держите локально, не публикуйте исходные выгрузки и их копии в общедоступных местах и не передавайте третьим лицам. Для общих тестов в репозитории используйте выдуманные данные.

Перед публикацией посмотрите `git status --short`. В панели **Source Control** в VS Code нажмите **Stage Changes** у каждого проверенного файла. В терминале можно добавить те же файлы через `git add --`, перечислив их имена. Проверьте подготовленный список командой `git diff --cached --name-only`. В пустом репозитории владелец после ревью публикует первый коммит в `main`. Если `main` уже существует, создайте ветку `codex/foundation` и после публикации откройте PR в `main`. Возможное сообщение для готового фундамента — `chore: bootstrap Python submission project`. При завершении RAG — `feat: add EnergyTool RAG consultation pipeline`. [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)

Для ручной публикации пустого репозитория после подготовки и проверки списка файлов выполните `git commit -m "chore: bootstrap Python submission project"`, затем `git push -u origin main`. Для существующего `main` создайте ветку командой `git switch -c codex/foundation` до коммита и укажите `codex/foundation` в команде push. Codex выполняет каждое из этих действий только после вашего прямого указания.

Когда базовые контракты появились в `main`, каждый участник обновляет `main` и создаёт свою ветку. Например, `codex/runtime` для запуска, `codex/retrieval` для поиска и `codex/consultant` для консультации. После прямого разрешения на публикацию изменений создайте на GitHub pull request с базой `main`. В описание добавьте цель, фактически выполненные проверки и оставшиеся ограничения. Интегратор проверяет интерфейсы и запускает общий прогон перед слиянием. [GitHub Docs по созданию pull request](https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-a-pull-request)

## 4. Сдать первый архив

Соберите отдельный ZIP из явно перечисленных корневых файлов по ТЗ. ZIP для судьи отличается по составу от репозитория. Пройдите [ручную проверку](TODAY-CHECKLIST.md) и загрузите ZIP в разделе сабмитов секции МТС. URL кнопки загрузки в приложенном техрегламенте отсутствует. Уточните путь на платформе у организаторов, если раздел недоступен в личном кабинете.
