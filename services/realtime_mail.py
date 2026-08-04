from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import smtplib
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config import BASE_DIR, DATA_DIR
from database import realtime_repository
from services.backtest.code_strategies import get_code_strategy


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
RETRY_DELAYS = (10, 30, 120)


class MailError(RuntimeError):
    def __init__(self, message: str, *, code: str, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.transient = transient


def _master_key() -> bytes:
    configured = os.getenv("REALTIME_MASTER_KEY", "").strip()
    if configured:
        return configured.encode("ascii")
    key_file = DATA_DIR / ".realtime-master.key"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def encrypt_secret(secret: str) -> str:
    return Fernet(_master_key()).encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return Fernet(_master_key()).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise MailError("邮件通道密钥无法解密，请重新填写授权码。", code="SECRET_UNAVAILABLE") from exc


def normalize_recipients(value) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,;\s]+", value)
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result = []
    for raw in values:
        item = str(raw or "").strip()
        if item and item not in result:
            if not EMAIL_PATTERN.fullmatch(item):
                raise ValueError(f"收件邮箱格式不正确：{item}")
            result.append(item)
    if not result:
        raise ValueError("至少需要一个收件邮箱。")
    return result


def default_channel_payload(provider: str, sender_email: str, name: str) -> dict:
    provider = provider.strip().lower()
    if provider == "qq_smtp":
        return {
            "name": name,
            "provider": provider,
            "sender_email": sender_email,
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "security_mode": "ssl",
            "username": sender_email,
        }
    if provider == "gmail_smtp":
        return {
            "name": name,
            "provider": provider,
            "sender_email": sender_email,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 465,
            "security_mode": "ssl",
            "username": sender_email,
        }
    raise ValueError("不支持的邮件供应商。")


def bootstrap_env_qq_channel() -> dict | None:
    """Seed the user's local QQ env credentials without returning the secret."""
    address = os.getenv("QQ_ADDRESS", "").strip()
    authorization = os.getenv("QQ_AUTHORIZATION_CODE", "").strip()
    if not address or not authorization or not EMAIL_PATTERN.fullmatch(address):
        return None
    channels = []
    try:
        channels = [realtime_repository.get_email_channel(item["id"]) for item in _list_channels()]
    except Exception:
        channels = []
    existing = next((item for item in channels if item["sender_email"].lower() == address.lower()), None)
    payload = default_channel_payload("qq_smtp", address, "QQ邮箱（环境配置）")
    cipher = encrypt_secret(authorization)
    if existing:
        return realtime_repository.update_email_channel(existing["id"], payload, secret_ciphertext=cipher)
    return realtime_repository.create_email_channel(payload, secret_ciphertext=cipher)


def _list_channels() -> list[dict]:
    from database.db import get_connection
    with get_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT id FROM email_channels ORDER BY id")]


def _channel_secret(channel_id: int) -> tuple[dict, str]:
    channel, ciphertext = realtime_repository.get_email_channel_secret(channel_id)
    if not ciphertext:
        raise MailError("邮件通道未配置授权码。", code="SECRET_MISSING")
    return channel, decrypt_secret(ciphertext)


def send_smtp(channel_id: int, *, recipient: str, subject: str, body: str, timeout: int = 20) -> str:
    if not EMAIL_PATTERN.fullmatch(recipient):
        raise MailError("收件邮箱格式不正确。", code="INVALID_RECIPIENT")
    channel, secret = _channel_secret(channel_id)
    message = EmailMessage()
    message["From"] = channel["sender_email"]
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc), usegmt=True)
    message["X-Mailer"] = "Trade Robot realtime decision"
    message["Message-ID"] = f"<{hashlib.sha256((str(channel_id) + recipient + subject + body).encode()).hexdigest()}@trade-robot.local>"
    message.set_content(body)
    try:
        if channel["security_mode"] == "ssl":
            with smtplib.SMTP_SSL(channel["smtp_host"], int(channel["smtp_port"]), timeout=timeout) as smtp:
                smtp.login(channel["username"], secret)
                response = smtp.send_message(message)
        else:
            with smtplib.SMTP(channel["smtp_host"], int(channel["smtp_port"]), timeout=timeout) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(channel["username"], secret)
                response = smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("邮件认证失败，请检查授权码。", code="AUTH_FAILED") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError("收件邮箱被邮件服务器拒绝。", code="RECIPIENT_REFUSED") from exc
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError) as exc:
        raise MailError("邮件网络连接失败。", code="NETWORK_ERROR", transient=True) from exc
    except smtplib.SMTPException as exc:
        code = getattr(exc, "smtp_code", None)
        transient = code is None or int(code) >= 400 and int(code) < 500
        raise MailError("SMTP 服务返回错误。", code="SMTP_ERROR", transient=transient) from exc
    return str(response or "accepted")


def _format_number(value) -> str:
    number = float(value)
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _decision_template_text(recommendations: list[dict]) -> str:
    if not recommendations:
        return "无新调仓决策"
    return "\n".join(
        f"{item['action']} {item['symbol']} "
        f"{_format_number(item.get('target_weight_percent', 0))}%"
        for item in recommendations
    )


def _basis_template_text(engine_logs: list[dict]) -> str:
    matched_logs = [
        log
        for log in engine_logs
        if log.get("event_type") == "RULE_EVALUATION"
        and (log.get("context") or {}).get("matched")
    ]
    if not matched_logs:
        return "没有非代码规则命中"
    lines = []
    for log in matched_logs:
        context = log.get("context") or {}
        inputs = context.get("inputs") or {}
        values = "，".join(
            f"{name}={_format_number(value)}"
            for name, value in inputs.items()
        )
        rule_name = context.get("rule_name") or context.get("rule_id") or "规则"
        condition = context.get("condition") or "true"
        detail = f"{log.get('symbol') or '-'}：{rule_name}（{condition}）"
        if values:
            detail += f"；{values}"
        lines.append(detail)
    return "\n".join(lines)


def render_message(task: dict, result: dict) -> tuple[str, str]:
    decision = result["decision"]
    event = decision["event"]
    recommendations = decision.get("recommendations") or []
    strategy_snapshot = task.get("strategy_snapshot") or {}
    engine_logs = result.get("calculation", {}).get("engine_logs") or []
    decision_text = _decision_template_text(recommendations)
    basis_text = _basis_template_text(engine_logs)
    code_key = strategy_snapshot.get("code_key")
    params = strategy_snapshot.get("definition", {}).get("params", {})
    lines = [
        f"{task['name']} · {decision['trading_date']} {event}",
        "",
    ]
    if strategy_snapshot.get("design_mode") == "code":
        strategy_type = get_code_strategy(code_key or "")
        if code_key == "rapid_drop_atr_rotation" and event == params.get("risk_check_time"):
            lines.append("急跌风险检查：按百分比/ATR 单日急跌规则排查当日需回避的标的。")
        else:
            lines.append(strategy_type.realtime_notification_intro())

    if code_key == "rapid_drop_atr_rotation" and event == params.get("risk_check_time"):
        risk_logs = [log for log in engine_logs if log.get("event_type") == "RAPID_DROP_ATR_RISK_CHECK"]
        lines.extend(["", "风险检查结果："])
        if risk_logs:
            lines.extend(f"- {log.get('message', '')}" for log in risk_logs[:12])
        else:
            lines.append("- 本次事件没有生成风险检查明细。")
        lines.extend(["", "持仓处置："])
        if recommendations:
            for item in recommendations[:12]:
                lines.append(
                    f"- {item['action']} {item['symbol']}，目标仓位 {item['target_weight_percent']:.2f}%；{item['reason']}"
                )
        else:
            lines.append("- 没有已持仓标的触发风险卖出。")
    elif code_key == "rapid_drop_atr_rotation" and event == params.get("selection_time"):
        score_logs = [log for log in engine_logs if log.get("event_type") == "RAPID_DROP_ATR_DAILY_SCORE"]
        score_logs.sort(key=lambda log: (
            (log.get("context") or {}).get("rank") is None,
            (log.get("context") or {}).get("rank") or 9999,
            str(log.get("symbol") or ""),
        ))
        lines.extend(["", "ATR 动量评分与排名："])
        if score_logs:
            lines.extend(f"- {log.get('message', '')}" for log in score_logs[:12])
        else:
            lines.append("- 本次事件没有生成有效 ATR 动量评分。")
        lines.extend(["", "轮动与调仓建议："])
        if recommendations:
            for item in recommendations[:12]:
                lines.append(
                    f"- {item['action']} {item['symbol']}，目标仓位 {item['target_weight_percent']:.2f}%，"
                    f"有效杠杆 {item['effective_leverage']:.2f}×；{item['reason']}"
                )
        else:
            lines.append("- 目标持仓未变化，无需调仓。")
    else:
        lines.extend(["", "建议："])
        if recommendations:
            for item in recommendations[:12]:
                lines.append(
                    f"- {item['action']} {item['symbol']}，目标仓位 {item['target_weight_percent']:.2f}%，"
                    f"有效杠杆 {item['effective_leverage']:.2f}×；{item['reason']}"
                )
        else:
            lines.append("- 当前事件没有新的调仓建议。")
    if strategy_snapshot.get("design_mode") == "visual":
        lines.extend(["", "决策依据：", *[f"- {item}" for item in basis_text.splitlines()]])
    warnings = decision.get("data_warnings") or result.get("data_manifest", {}).get("missing") or []
    if warnings:
        lines.extend(["", "数据提示：", *[f"- 已忽略：{item}" for item in warnings[:8]]])
    if strategy_snapshot.get("design_mode") == "code" and code_key != "rapid_drop_atr_rotation":
        calculations = []
        for log in engine_logs:
            if log.get("event_type") in {"RAPID_DROP_ATR_DAILY_SCORE", "SEVENSTAR_DAILY_SCORE"}:
                calculations.append(str(log.get("message", "")))
        if calculations:
            lines.extend(["", "评分摘要：", *[f"- {item}" for item in calculations[:12]]])
    lines.extend([
        "",
        "本邮件为实时决策辅助信息，需人工核验行情、风险和实际持仓后再决定是否交易。",
    ])
    event_label = event
    if code_key == "rapid_drop_atr_rotation":
        if event == params.get("risk_check_time"):
            event_label = f"风险检查 {event}"
        elif event == params.get("selection_time"):
            event_label = f"轮动决策 {event}"
    default_subject = f"实时决策｜{task['name']}｜{decision['trading_date']} {event_label}"
    settings = task.get("notification_settings") or {}
    subject_template = str(settings.get("subject_template") or default_subject)
    body_template = str(settings.get("body_template") or "")
    replacements = {
        "{{task.name}}": task["name"],
        "{{event}}": f"{decision['trading_date']} {event}",
        "{{event.name}}": event,
        "{{date}}": decision["trading_date"],
        "{{decision}}": decision_text,
        "{{basis}}": basis_text,
        "{{summary}}": "\n".join(lines),
    }
    for key, value in replacements.items():
        subject_template = subject_template.replace(key, str(value))
        body_template = body_template.replace(key, str(value))
    return subject_template[:200], (body_template.strip() or "\n".join(lines))[:20000]


class NotificationDispatcher:
    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="realtime-mail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def enqueue_for_event(self, task: dict, event: dict, result: dict) -> int:
        settings = task.get("notification_settings") or {}
        if not settings.get("enabled"):
            return 0
        channel_id = settings.get("channel_id")
        if not channel_id:
            raise MailError("已启用邮件但未选择邮件通道。", code="CHANNEL_MISSING")
        recipients = normalize_recipients(settings.get("recipients"))
        subject, body = render_message(task, result)
        inserted = 0
        allowed = realtime_repository.reserve_normal_send(
            task["id"], now=datetime.now(timezone.utc), cooldown_seconds=60
        )
        if not allowed:
            return 0
        for recipient in recipients:
            dedupe = f"event:{event['id']}:channel:{channel_id}:recipient:{recipient}"
            realtime_repository.create_notification(
                event_id=event["id"], task_id=task["id"], channel_id=int(channel_id),
                recipient=recipient, subject=subject, body=body, dedupe_key=dedupe,
            )
            inserted += 1
        self.wake()
        return inserted

    def _loop(self) -> None:
        while not self._stop.is_set():
            pending = realtime_repository.list_pending_notifications(limit=20)
            if not pending:
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            for notification in pending:
                if self._stop.is_set():
                    return
                self._send_one(notification)

    def _send_one(self, notification: dict) -> None:
        attempt_no = int(notification.get("attempt_count") or 0) + 1
        attempt_id = realtime_repository.create_notification_attempt(notification["id"], attempt_no)
        realtime_repository.update_notification(notification["id"], status="sending", attempt_count=attempt_no)
        try:
            provider_id = send_smtp(
                int(notification["channel_id"]), recipient=notification["recipient"],
                subject=notification["subject"], body=notification["body"],
            )
        except MailError as exc:
            retry = exc.transient and attempt_no <= len(RETRY_DELAYS)
            status = "retrying" if retry else "failed"
            next_at = None
            if retry:
                next_at = (datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAYS[attempt_no - 1])).replace(microsecond=0).isoformat()
            realtime_repository.update_notification(
                notification["id"], status=status, next_attempt_at=next_at,
                error_code=exc.code, error_message=str(exc),
            )
            realtime_repository.finish_notification_attempt(
                attempt_id, status=status, error_code=exc.code, error_message=str(exc),
            )
            return
        realtime_repository.update_notification(
            notification["id"], status="sent", sent_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            provider_message_id=provider_id, error_code=None, error_message=None,
        )
        realtime_repository.finish_notification_attempt(
            attempt_id, status="sent", provider_message_id=provider_id,
        )
        realtime_repository.increment_successful_notifications(notification["task_id"])
