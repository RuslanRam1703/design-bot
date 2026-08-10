from aiogram.fsm.state import State, StatesGroup


class BriefStates(StatesGroup):
    awaiting_tz_file = State()
