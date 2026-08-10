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
