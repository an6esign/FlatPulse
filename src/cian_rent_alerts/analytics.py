from __future__ import annotations

EV_START = "start"
EV_SETUP_STARTED = "setup_started"
EV_CITY_SELECTED = "city_selected"
EV_FILTERS_COMPLETED = "filters_completed"
EV_INITIAL_SEARCH_SUCCESS = "initial_search_success"
EV_SHOW_FOUND_CLICKED = "show_found_clicked"
EV_SHOW_LATER_CLICKED = "show_later_clicked"
EV_TRIAL_OFFER_CLICKED = "trial_offer_clicked"
EV_TRIAL_STARTED = "trial_started"
EV_TRIAL_EXPIRED = "trial_expired"
EV_PAYMENT_OPENED = "payment_opened"
EV_PAYMENT_CREATED = "payment_created"
EV_PAYMENT_SUCCEEDED = "payment_succeeded"
EV_NOTIFICATIONS_STOPPED = "notifications_stopped"
EV_SEARCH_DELETED = "search_deleted"
EV_MANUAL_CHECK = "manual_check"
EV_MANUAL_CHECK_BLOCKED = "manual_check_blocked"
EV_PARSER_ERROR = "parser_error"
EV_CAPTCHA_ERROR = "captcha_error"
EV_EMPTY_PARSE_ERROR = "empty_parse_error"
EV_TELEGRAM_SEND_ERROR = "telegram_send_error"
EV_WEBHOOK_ERROR = "webhook_error"

FUNNEL_EVENTS = (
    (EV_START, "/start"),
    (EV_SETUP_STARTED, "начал настройку"),
    (EV_CITY_SELECTED, "выбрал город"),
    (EV_FILTERS_COMPLETED, "завершил фильтры"),
    (EV_INITIAL_SEARCH_SUCCESS, "первичный поиск успешен"),
    (EV_SHOW_FOUND_CLICKED, "нажал Показать квартиры"),
    (EV_SHOW_LATER_CLICKED, "нажал Посмотреть позже"),
    (EV_TRIAL_OFFER_CLICKED, "нажал Попробовать 7 дней"),
    (EV_TRIAL_STARTED, "trial started"),
    (EV_TRIAL_EXPIRED, "trial expired"),
    (EV_PAYMENT_OPENED, "payment opened"),
    (EV_PAYMENT_CREATED, "payment created"),
    (EV_PAYMENT_SUCCEEDED, "payment succeeded"),
    (EV_NOTIFICATIONS_STOPPED, "stopped notifications"),
    (EV_SEARCH_DELETED, "deleted search"),
)


def event_for_error_type(error_type: str) -> str:
    if error_type == "captcha":
        return EV_CAPTCHA_ERROR
    if error_type == "empty_parse":
        return EV_EMPTY_PARSE_ERROR
    if error_type == "telegram":
        return EV_TELEGRAM_SEND_ERROR
    return EV_PARSER_ERROR
