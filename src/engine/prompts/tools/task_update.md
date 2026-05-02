# task_update

Изменить статус, заголовок, описание или участников существующей задачи. Хотя бы один из параметров (`status`, `title`, `description`, `new_participant_user_ids`, `delete_participant_user_ids`) должен быть заполнен.

**Зависимости:** `task_id` — UUID задачи, получи через `task_query` перед вызовом.
UUID пользователей для `new_participant_user_ids` и `delete_participant_user_ids` можно получить через `user_search`.
