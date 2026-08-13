"""Email notifications: a manually-triggered digest of every bug currently
found across all tracked shops. Sent over Gmail SMTP with an App Password
(https://myaccount.google.com/apppasswords) -- read from environment
variables so no credentials live in the repo.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "")


def render_bug_report(bugs_by_shop):
    """bugs_by_shop: [{"label": shop label, "zero_price": [...], "placeholder": [...]}, ...]"""
    total_zero = sum(len(s["zero_price"]) for s in bugs_by_shop)
    total_placeholder = sum(len(s["placeholder"]) for s in bugs_by_shop)

    text = [f"Bug summary across {len(bugs_by_shop)} shops", ""]
    html = [
        f"<h2>Bug summary across {len(bugs_by_shop)} shops</h2>",
        f"<p>{total_zero} zero-priced listings, {total_placeholder} placeholder 30-day-low prices.</p>",
    ]

    for shop in bugs_by_shop:
        if not shop["zero_price"] and not shop["placeholder"]:
            continue
        text.append(f"== {shop['label']} ==")
        html.append(f"<h3>{shop['label']}</h3>")

        if shop["zero_price"]:
            text.append("Zero-priced listings:")
            html.append("<p><b>Zero-priced listings:</b></p><ul>")
            for it in shop["zero_price"]:
                text.append(f"  - {it['name']} ({it['category']})")
                html.append(f"<li>{it['name']} <i>({it['category']})</i></li>")
            html.append("</ul>")

        if shop["placeholder"]:
            text.append("Placeholder 30-day-low price (EUR 0.01):")
            html.append("<p><b>Placeholder 30-day-low price (&euro;0.01):</b></p><ul>")
            for it in shop["placeholder"]:
                text.append(f"  - {it['name']} ({it['category']}) now {it['price']:.2f} EUR")
                html.append(f"<li>{it['name']} <i>({it['category']})</i> now {it['price']:.2f}&euro;</li>")
            html.append("</ul>")

        text.append("")

    return "\n".join(text), "".join(html)


def send_bug_email(bugs_by_shop):
    if not (SMTP_USER and SMTP_PASSWORD and NOTIFY_EMAIL_TO):
        raise RuntimeError(
            "Email isn't configured: set the SMTP_USER, SMTP_PASSWORD, and "
            "NOTIFY_EMAIL_TO environment variables."
        )

    total_zero = sum(len(s["zero_price"]) for s in bugs_by_shop)
    total_placeholder = sum(len(s["placeholder"]) for s in bugs_by_shop)
    subject = (
        f"Masoutis dashboard: {total_zero + total_placeholder} pricing bugs found "
        f"({total_zero} zero-price, {total_placeholder} placeholder-price)"
    )
    text_body, html_body = render_bug_report(bugs_by_shop)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [NOTIFY_EMAIL_TO], msg.as_string())

    return {
        "sent_to": NOTIFY_EMAIL_TO,
        "subject": subject,
        "zero_price_count": total_zero,
        "placeholder_count": total_placeholder,
    }
