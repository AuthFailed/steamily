from aiogram.fsm.state import State, StatesGroup


class Menu(StatesGroup):
    language = State()
    main = State()
    login_intro = State()
    login = State()
    settings = State()
    group_alerts = State()
    logout = State()
    deleted = State()
    about = State()
    privacy = State()
    status = State()


class Games(StatesGroup):
    list = State()
    pages = State()
    sort = State()
    filters = State()
    budget = State()
    genre = State()
    players = State()
    game = State()


class Admin(StatesGroup):
    members = State()
    member = State()


class FamilyView(StatesGroup):
    info = State()
    slots = State()
    stats = State()
    stat = State()
    members = State()
    member = State()
    remove = State()
    manage = State()
    invite = State()
    invite_role = State()
    invites = State()
    rename = State()
    purchases = State()
    purchase = State()
    decline_purchase = State()
