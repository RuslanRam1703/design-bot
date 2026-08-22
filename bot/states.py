from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    add_case_category = State()
    add_case_title = State()
    add_case_photo = State()
    add_case_description = State()

    edit_case_pick = State()
    edit_case_field_pick = State()
    edit_case_value = State()

    delete_case_pick = State()
    delete_case_confirm = State()

    add_faq_question = State()
    add_faq_answer = State()

    edit_faq_pick = State()
    edit_faq_field_pick = State()
    edit_faq_value = State()

    delete_faq_pick = State()
    delete_faq_confirm = State()

    edit_about_field_pick = State()
    edit_about_value = State()
    edit_about_photo = State()

    add_service_name = State()
    add_service_price = State()
    add_service_term_min = State()
    add_service_term_max = State()
    add_service_includes = State()

    edit_service_pick = State()
    edit_service_field_pick = State()
    edit_service_value = State()

    delete_service_pick = State()
    delete_service_confirm = State()

    # Опции управляются внутри редактирования конкретной услуги — service_id
    # уже есть в data к этому моменту, отдельный "выбор услуги" не нужен.
    option_add_name = State()
    option_add_price = State()
    option_add_days = State()
    option_add_multipliable = State()

    option_edit_pick = State()
    option_edit_field_pick = State()
    option_edit_value = State()

    option_delete_pick = State()
    option_delete_confirm = State()

    edit_coefficients_pick = State()
    edit_coefficients_value = State()

    add_category_label = State()

    rename_category_pick = State()
    rename_category_value = State()

    category_related_service_pick = State()

    delete_category_pick = State()
    delete_category_confirm = State()

    # Изображения кейса (вложены в редактирование кейса, как опции — вложены
    # в редактирование услуги: case_id уже есть в data к этому моменту).
    case_images_menu = State()
    case_image_add = State()
    case_image_pick_delete = State()
    case_image_pick_cover = State()
    case_image_pick_reorder = State()

    # Разделы кейса (sections) — гибкое содержимое вместо жёстких
    # task/solution/result.
    case_sections_menu = State()
    case_section_add_type = State()
    case_section_add_title = State()
    case_section_add_content = State()
    case_section_pick_edit = State()
    case_section_edit_field_pick = State()
    case_section_edit_value = State()
    case_section_pick_delete = State()
    case_section_pick_reorder = State()

    change_case_category_pick = State()

    # Опыт работы в "Обо мне" (experience[]).
    about_experience_menu = State()
    about_experience_add_role = State()
    about_experience_add_company = State()
    about_experience_add_period = State()
    about_experience_add_description = State()
    about_experience_pick_delete = State()

    # Заявки (leads).
    leads_list = State()
    lead_detail = State()
    lead_reply_text = State()
    lead_delete_confirm = State()
    lead_materials_list = State()

    # Бэкап (экспорт/восстановление data/*.json + фото через .zip-файл в
    # Telegram) — на бесплатном Render файловая система эфемерна, это
    # ручной способ пережить redeploy без стороннего сервиса.
    backup_menu = State()
    backup_restore_wait_file = State()
    backup_restore_confirm = State()
