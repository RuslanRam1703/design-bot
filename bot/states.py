from aiogram.fsm.state import State, StatesGroup


class BriefStates(StatesGroup):
    awaiting_tz_file = State()


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

    delete_category_pick = State()
    delete_category_confirm = State()
