"""Explicit owner clean-start preparation; never reset Redis or delete Telegram media."""
import copy
import io
import json
import uuid
import zipfile

from bot import content_store as store


class ResetFailedError(RuntimeError):
    pass


def _digest(documents):
    return store.editor_digest(documents)


async def prepare_clean_start(actor):
    store._require_designer(actor)
    backup = await store.export_backup_bytes()
    with zipfile.ZipFile(io.BytesIO(backup)) as archive:
        documents = {name: json.loads(archive.read('data/' + name)) for name in store.DATA_FILENAMES}
    portfolio = documents['portfolio.json']; pricing = documents['pricing.json']; leads = documents['leads.json']
    counts = {
        'Кейсы': len(portfolio['cases']), 'Категории': len(portfolio['types']),
        'Услуги': len(pricing['services']), 'Опции': len(pricing['options']),
        'FAQ': len(documents['faq.json']['faq']), 'Заявки': len(leads['leads']),
        'Проекты': len(leads.get('projects', [])),
        'Записи об оплатах': sum(len(p.get('payments', [])) for p in leads.get('projects', [])),
    }
    return backup, _digest(documents), counts


async def clean_start(actor, expected_digest):
    """Clear only application content after a matching backup was delivered.

    Connections, permissions/menu config, Redis itself, topic mappings and media
    remain intact. Keeping the ID watermark prevents old topics addressing new clients.
    """
    store._require_designer(actor)
    async with store._lock(*store.DATA_FILENAMES):
        previous = {name: await store._read(name) for name in store.DATA_FILENAMES}
        if _digest(previous) != expected_digest:
            raise ValueError('Данные изменились после создания копии. Подготовьте очистку заново.')
        watermark = max([previous['leads.json'].get('lead_id_highwater', 0)] + [l['id'] for l in previous['leads.json']['leads']])
        replacements = {
            'portfolio.json': {'cases': [], 'types': [], 'gallery_demo_seeded_v1': True},
            'pricing.json': {'services': [], 'options': [], 'groups': [],
                'coefficients': {'urgent': {'multiplier': 1}, 'complex': {'multiplier': 1}},
                'rounding': {'price_from_factor': 1, 'price_to_factor': 1, 'round_to': 1}},
            'faq.json': {'faq': []}, 'about.json': {},
            'leads.json': {'leads': [], 'projects': [], 'lead_id_highwater': watermark, 'access_control': copy.deepcopy(previous['leads.json'].get('access_control', {}))},
            'ui_config.json': {**copy.deepcopy(previous['ui_config.json']), 'content_epoch': uuid.uuid4().hex},
        }
        attempted = []
        try:
            for name in store.DATA_FILENAMES:
                attempted.append(name)
                await store._write(name, replacements[name])
        except Exception as exc:
            failed = []
            for name in reversed(attempted):
                try:
                    await store._write(name, previous[name])
                except Exception:
                    failed.append(name)
            if failed:
                raise ResetFailedError('Очистка прервана; восстановление выполнено не полностью. Не добавляйте данные до восстановления из выданной копии.') from exc
            raise ResetFailedError('Очистка отменена из-за ошибки записи. Исходные данные восстановлены.') from exc


async def delete_lead_with_topic(actor, lead_id, bot):
    """Explicit owner action. A Telegram failure never silently drops the lead."""
    import asyncio
    from aiogram.exceptions import TelegramBadRequest
    from bot import config
    store._require_designer(actor)
    if not config.CLIENT_MATERIALS_CHANNEL or not store.is_redis_backed():
        raise ValueError('Архив не подключён. Можно удалить только запись приложения; тема автоматически не удалена.')
    chat_id = int(config.CLIENT_MATERIALS_CHANNEL)
    async with store._lock('leads.json'):
        document = await store._read('leads.json')
        lead = next((l for l in document['leads'] if l['id'] == lead_id), None)
        if lead is None:
            return False
        if any(p.get('lead_id') == lead_id for p in document.get('projects', [])):
            raise ValueError('Заявка связана с проектом. Удаление не выполнено; финансовая история сохранена.')
        key = f'design-bot:client-materials:topic:{chat_id}:{lead_id}'
        topic = await asyncio.to_thread(store._upstash_command, 'GET', key)
        if topic is not None:
            try:
                removed = await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=int(topic))
                if removed is not True:
                    raise ValueError('Telegram не подтвердил удаление темы. Заявка сохранена.')
            except TelegramBadRequest as exc:
                if not any(token in str(exc).lower() for token in ('message thread not found', 'topic_id_invalid', 'topic_deleted')):
                    raise ValueError('Не удалось удалить тему. Проверьте права бота. Заявка сохранена.') from exc
        await store._write_leads([l for l in document['leads'] if l['id'] != lead_id])
        # Old mappings are inert: IDs are never reused. Keeping the mapping also
        # avoids losing recovery information after a storage/network failure.
        return True
