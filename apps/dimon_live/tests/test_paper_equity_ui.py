from pathlib import Path


STATIC = Path(__file__).parents[1] / "static"


def test_real_order_card_uses_quote_and_funding_currencies_not_format_sgd_everywhere():
    source = (STATIC / "paper_equity_ui.mjs").read_text()
    assert "quote_currency" in source
    assert "funding_currency" in source
    assert "max_funding_debit_minor" in source


def test_confirmation_button_starts_singpass_then_face_device():
    source = (STATIC / "app.js").read_text()
    assert "paperSingpass" in source
    assert "/api/face/verify" in source
    assert "requestPaperEquityApproval" in source


def test_paper_order_face_step_opens_visible_confirmation_dialog():
    source = (STATIC / "app.js").read_text()
    assert "confirmPaperFace" in source
    assert "PAPER_APPROVAL_CANCELLED" in source
    assert "ui.approvalDialog.showModal()" in source


def test_paper_singpass_uses_the_visible_mock_app_dialog():
    source = (STATIC / "invest_ui.mjs").read_text()
    assert 'window.open("/mock-singpass"' in source
    assert "requestUrl" in source
    assert "requestBody" in source


def test_paper_order_approval_button_cannot_create_duplicate_singpass_requests():
    source = (STATIC / "paper_equity_ui.mjs").read_text()
    assert "button.disabled = true" in source
    assert "Opening approval" in source


def test_voice_yes_never_calls_an_approval_or_execution_endpoint():
    source = (STATIC / "app.js").read_text()
    assert "activeConversation?.status === \"WAITING\"" in source
    assert "approvePaperOrder" in source
    assert "ORDER_CONFIRMATION_REQUEST" in source


def test_automatic_microphone_reopen_stops_at_immutable_receipt():
    source = (STATIC / "app.js").read_text()
    assert "pendingOnScreenReview = Boolean" in source
    assert "speakAutomatically && !pendingOnScreenReview" in source
    assert "paper_order" in source


def test_filled_open_queued_and_requote_states_have_honest_copy():
    source = (STATIC / "app.js").read_text()
    assert 'paper.status === "FILLED"' in source
    assert 'paper.status === "QUEUED" || paper.status === "OPEN"' in source
    assert "Your balance and holdings haven’t changed" in source
    assert "Nothing was bought and your balance is unchanged" in source


def test_input_and_order_result_remain_in_one_screenshot_region():
    html = (STATIC / "index.html").read_text()
    assert 'id="command-form"' in html
    assert 'id="candidate-list"' in html
