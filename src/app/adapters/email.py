"""email_utils.py: Email utility functions for sending emails"""

import html as html_mod
import smtplib
from datetime import UTC, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.constants import (
    EMAIL_CONFIG,
    ENVIRONMENT,
    ERROR_ALERT_EMAILS,
    ERROR_ALERT_SENDER_EMAIL,
    ERROR_ALERT_SENDER_EMAIL_PASS,
    ERROR_DIGEST_INTERVAL_SECONDS,
    FORGOT_PASSWORD_EXPIRE_MINUTES,
    PAYMENT_ALERT_EMAILS,
    PAYMENT_ALERT_SENDER_EMAIL,
    PAYMENT_ALERT_SENDER_EMAIL_PASS,
    SIGNUP_VERIFICATION_EXPIRE_MINUTES,
)
from app.core.logging import setup_logging

# Configure logging
logger = setup_logging(__file__)


class EmailConfig:
    """Email configuration class"""

    def __init__(
        self,
        smtp_server: str = EMAIL_CONFIG["SMTP_SERVER"],
        smtp_port: int = EMAIL_CONFIG["SMTP_PORT"],
        sender_email: str = EMAIL_CONFIG["SENDER_EMAIL"],
        app_password: str = EMAIL_CONFIG["APP_PASSWORD"],
        use_tls: bool = EMAIL_CONFIG["USE_TLS"],
        cc_emails: list[str] | None = None,
        bcc_emails: list[str] | None = None,
    ):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = sender_email
        self.app_password = app_password
        self.use_tls = use_tls
        self.cc_emails = cc_emails
        self.bcc_emails = bcc_emails


def send_email(
    email_config: EmailConfig,
    recipient_emails: list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
    message: MIMEMultipart | None = None,
) -> bool:
    """
    Send an email using SMTP with app password authentication.

    Args:
        email_config (EmailConfig): Email configuration object
        recipient_emails (List[str]): List of recipient email addresses
        subject (str): Email subject
        body (str): Plain text email body
        html_body (Optional[str]): HTML email body
        cc_emails (Optional[List[str]]): List of CC email addresses (overrides default)
        bcc_emails (Optional[List[str]]): List of BCC email addresses (overrides default)
        message (Optional[MIMEMultipart]): Pre-created message with attachments

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    try:
        # Use provided message or create new one
        if message is None:
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = email_config.sender_email
            message["To"] = ", ".join(recipient_emails)

            # Add CC and BCC if configured
            cc_emails = cc_emails or email_config.cc_emails
            bcc_emails = bcc_emails or email_config.bcc_emails

            if cc_emails:
                message["Cc"] = ", ".join(cc_emails)

            # Add plain text body
            message.attach(MIMEText(body, "plain"))

            # Add HTML body if provided
            if html_body:
                message.attach(MIMEText(html_body, "html"))
        else:
            # Set headers for pre-created message
            message["Subject"] = subject
            message["From"] = email_config.sender_email
            message["To"] = ", ".join(recipient_emails)
            # Add CC and BCC if configured
            cc_emails = cc_emails or email_config.cc_emails
            bcc_emails = bcc_emails or email_config.bcc_emails

            if cc_emails:
                message["Cc"] = ", ".join(cc_emails)

        # Combine all recipients
        all_recipients = recipient_emails.copy()
        if cc_emails:
            all_recipients.extend(cc_emails)
        if bcc_emails:
            all_recipients.extend(bcc_emails)

        # Send email
        with smtplib.SMTP(email_config.smtp_server, email_config.smtp_port) as smtp:
            if email_config.use_tls:
                smtp.starttls()
            smtp.login(email_config.sender_email, email_config.app_password)
            smtp.send_message(message, to_addrs=all_recipients)

        logger.info(f"Email sent successfully to {', '.join(recipient_emails)}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email: {e!s}")
        return False


def send_report_notification_email(
    recipient_email: str,
    user_name: str,
    report_title: str,
    email_config: EmailConfig = EmailConfig(
        cc_emails=EMAIL_CONFIG["CC_EMAILS"], bcc_emails=EMAIL_CONFIG["BCC_EMAILS"]
    ),
    attachment_data: list[dict[str, str]] | None = [],
    report_generation_time: datetime = datetime.now(UTC),
) -> bool:
    """
    Send a report notification email with a beautiful HTML template and file attachments.

    Args:
        recipient_email (str): Recipient email address
        user_name (str): Name of the user
        report_title (str): Title of the generated report
        email_config (EmailConfig): Email configuration object
        attachments (Optional[Dict[str, str]]): Dictionary of file paths to attach
            Format: {
                "file_path": "path/to/file",
                "mime_type": "application/pdf" or "text/html" or "text/markdown"
            }

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    if ENVIRONMENT == "DEV":
        subject = f"DEV - Your Report: {report_title}"
    else:
        subject = f"Your Report: {report_title}"

    # Plain text body
    body = f"""Hello {user_name or "User"},

            Your requested report "{report_title}" has been generated at {report_generation_time.strftime("%Y-%m-%d %H:%M:%S")} (UTC) and is attached to this email.

            Thank you for using Caspr.!

            Best Regards,
            Team Caspr.
            """

    # HTML body with beautiful coral theme styling
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                line-height: 1.6;
                color: #333333;
                margin: 0;
                padding: 0;
                background-color: #fdf2f2;
            }}
            .container {{
                max-width: 600px;
                margin: 20px auto;
                background-color: #ffffff;
                border-radius: 10px;
                box-shadow: 0 2px 5px rgba(245, 145, 145, 0.2);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #C91B1A 0%, #A01616 100%);
                color: white;
                padding: 30px 20px;
                text-align: center;
            }}
            .header h1 {{
                margin: 0;
                font-size: 24px;
                font-weight: 600;
            }}
            .content {{
                padding: 30px 20px;
            }}
            .message {{
                margin-bottom: 25px;
                font-size: 16px;
            }}
            .report-info {{
                background-color: #fef7f7;
                border-left: 4px solid #F59191;
                padding: 15px;
                margin: 20px 0;
                border-radius: 0 5px 5px 0;
            }}
            .report-title {{
                color: #D65A5A;
                font-weight: 600;
                margin-bottom: 10px;
            }}
            .timestamp {{
                color: #8B5A5A;
                font-size: 14px;
            }}
            .footer {{
                background-color: #fef7f7;
                padding: 20px;
                text-align: center;
                border-top: 1px solid #F5C2C2;
            }}
            .footer p {{
                margin: 5px 0;
                color: #8B5A5A;
                font-size: 14px;
            }}
            .logo {{
                font-size: 28px;
                font-weight: bold;
                color: white;
                margin-bottom: 10px;
            }}
            .attachments {{
                margin-top: 20px;
                padding: 15px;
                background-color: #fef7f7;
                border-radius: 5px;
            }}
            .attachments h3 {{
                color: #D65A5A;
                margin-top: 0;
                font-size: 16px;
            }}
            .attachments ul {{
                list-style: none;
                padding: 0;
                margin: 10px 0;
            }}
            .attachments li {{
                margin: 5px 0;
                color: #8B5A5A;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">Caspr.</div>
                <h1>Your Report is Ready!</h1>
            </div>
            <div class="content">
                <div class="message">
                    Hello {user_name or "User"},
                </div>
                <div class="message">
                    We're pleased to inform you that your requested report has been generated successfully.
                </div>
                <div class="report-info">
                    <div class="report-title">Report: {report_title}</div>
                    <div class="timestamp">Generated at: {report_generation_time.strftime("%Y-%m-%d %H:%M:%S")} (UTC)</div>
                </div>
                <div class="message">
                    Thank you for using Caspr.! If you have any questions or need assistance, please don't hesitate to contact our support team.
                </div>
            </div>
            <div class="footer">
                <p>Best Regards,</p>
                <p>Team Caspr.</p>
                <p style="margin-top: 15px; font-size: 12px; color: #A67373;">
                    This is an automated message, please do not reply directly to this email.
                </p>
            </div>
        </div>
    </body>
    </html>
    """

    # Create message with attachments
    message = MIMEMultipart("mixed")

    # Create the alternative part for text and HTML
    alternative_part = MIMEMultipart("alternative")
    message.attach(alternative_part)

    # Add plain text body
    alternative_part.attach(MIMEText(body, "plain"))

    # Add HTML body
    alternative_part.attach(MIMEText(html_body, "html"))

    # Add attachments if provided
    if attachment_data:
        for attachment in attachment_data:
            file_data = attachment.get("file_data")
            mime_type = attachment.get("mime_type")
            file_name = attachment.get("file_name")

            # Create attachment
            attachment = MIMEApplication(file_data, _subtype=mime_type.split("/")[-1])
            attachment.add_header("Content-Disposition", "attachment", filename=file_name)
            message.attach(attachment)

    # Send email using the existing send_email function
    return send_email(
        email_config=email_config,
        recipient_emails=[recipient_email],
        subject=subject,
        body=body,
        html_body=html_body,
        message=message,  # Pass the message with attachments
    )


def send_password_reset_email(
    recipient_email: str,
    user_name: str,
    reset_link: str,
    email_config: EmailConfig = EmailConfig(
        cc_emails=EMAIL_CONFIG["CC_EMAILS"], bcc_emails=EMAIL_CONFIG["BCC_EMAILS"]
    ),
) -> bool:
    """
    Send a password reset email with a beautiful HTML template and reset link button.

    Args:
        recipient_email (str): Recipient email address
        user_name (str): Name of the user
        reset_link (str): Frontend reset link URL
        email_config (EmailConfig): Email configuration object

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    if ENVIRONMENT == "DEV":
        subject = "DEV - Password Reset Request"
    else:
        subject = "Password Reset Request"

    # Calculate expiration time in minutes/hours for better readability
    expiration_time = FORGOT_PASSWORD_EXPIRE_MINUTES
    expiration_text = f"{expiration_time} minutes"
    if expiration_time >= 60:
        hours = expiration_time / 60
        expiration_text = f"{hours:.1f} hours" if hours % 1 != 0 else f"{int(hours)} hours"

    # Plain text body
    body = f"""Hello {user_name or "User"},

            You have requested to reset your password. Please click the link below to reset your password.
            
            This link will expire in {expiration_text}.
            
            {reset_link}
            
            If you did not request a password reset, please ignore this email.

            Best Regards,
            Team Caspr.
            """

    # HTML body with beautiful coral theme styling
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                line-height: 1.6;
                color: #333333;
                margin: 0;
                padding: 0;
                background-color: #fdf2f2;
            }}
            .container {{
                max-width: 600px;
                margin: 20px auto;
                background-color: #ffffff;
                border-radius: 10px;
                box-shadow: 0 2px 5px rgba(245, 145, 145, 0.2);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #C91B1A 0%, #A01616 100%);
                color: white;
                padding: 30px 20px;
                text-align: center;
            }}
            .header h1 {{
                margin: 0;
                font-size: 24px;
                font-weight: 600;
            }}
            .content {{
                padding: 30px 20px;
            }}
            .message {{
                margin-bottom: 25px;
                font-size: 16px;
            }}
            .button-container {{
                text-align: center;
                margin: 30px 0;
            }}
            .button {{
                display: inline-block;
                background: linear-gradient(135deg, #C91B1A 0%, #A01616 100%);
                color: white;
                text-decoration: none;
                padding: 12px 30px;
                border-radius: 5px;
                font-weight: 600;
                letter-spacing: 0.5px;
            }}
            .expiry-notice {{
                background-color: #fef7f7;
                border-left: 4px solid #F59191;
                padding: 15px;
                margin: 20px 0;
                border-radius: 0 5px 5px 0;
                font-size: 14px;
                color: #8B5A5A;
            }}
            .footer {{
                background-color: #fef7f7;
                padding: 20px;
                text-align: center;
                border-top: 1px solid #F5C2C2;
            }}
            .footer p {{
                margin: 5px 0;
                color: #8B5A5A;
                font-size: 14px;
            }}
            .logo {{
                font-size: 28px;
                font-weight: bold;
                color: white;
                margin-bottom: 10px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">Caspr.</div>
                <h1>Password Reset Request</h1>
            </div>
            <div class="content">
                <div class="message">
                    Hello {user_name or "User"},
                </div>
                <div class="message">
                    We received a request to reset your password. Please click the button below to create a new password.
                </div>
                <div class="button-container">
                    <a href="{reset_link}" class="button" style="color: white;">Reset Password</a>
                </div>
                <div class="expiry-notice">
                    <strong>Note:</strong> This password reset link will expire in {expiration_text}.
                </div>
                <div class="message">
                    If you didn't request a password reset, please ignore this email or contact our support team if you have concerns.
                </div>
            </div>
            <div class="footer">
                <p>Best Regards,</p>
                <p>Team Caspr.</p>
                <p style="margin-top: 15px; font-size: 12px; color: #A67373;">
                    This is an automated message, please do not reply directly to this email.
                </p>
            </div>
        </div>
    </body>
    </html>
    """

    # Create message with HTML part
    message = MIMEMultipart("alternative")

    # Add plain text body
    message.attach(MIMEText(body, "plain"))

    # Add HTML body
    message.attach(MIMEText(html_body, "html"))

    # Send email using the existing send_email function
    return send_email(
        email_config=email_config,
        recipient_emails=[recipient_email],
        subject=subject,
        body=body,
        html_body=html_body,
        message=message,
    )


def send_signup_verification_email(
    recipient_email: str,
    user_name: str,
    verification_link: str,
    email_config: EmailConfig = EmailConfig(
        cc_emails=EMAIL_CONFIG["CC_EMAILS"], bcc_emails=EMAIL_CONFIG["BCC_EMAILS"]
    ),
) -> bool:
    """
    Send an email verification email with a beautiful HTML template and verification link button.

    Args:
        recipient_email (str): Recipient email address
        user_name (str): Name of the user
        verification_link (str): Frontend verification link URL
        email_config (EmailConfig): Email configuration object

    Returns:
        bool: True if email was sent successfully, False otherwise
    """

    if ENVIRONMENT == "DEV":
        subject = "DEV - Verify Your Email Address"
    else:
        subject = "Verify Your Email Address"

    # Calculate expiration time in minutes/hours for better readability
    expiration_time = SIGNUP_VERIFICATION_EXPIRE_MINUTES
    expiration_text = f"{expiration_time} minutes"
    if expiration_time >= 60:
        hours = expiration_time / 60
        expiration_text = f"{hours:.1f} hours" if hours % 1 != 0 else f"{int(hours)} hours"

    # Plain text body
    body = f"""Hello {user_name or "User"},

            Thank you for signing up! Please verify your email address by clicking the link below.
            
            This link will expire in {expiration_text}.
            
            {verification_link}
            
            If you did not create an account, please ignore this email.

            Best Regards,
            Team Caspr.
            """

    # HTML body with beautiful coral theme styling
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                line-height: 1.6;
                color: #333333;
                margin: 0;
                padding: 0;
                background-color: #fdf2f2;
            }}
            .container {{
                max-width: 600px;
                margin: 20px auto;
                background-color: #ffffff;
                border-radius: 10px;
                box-shadow: 0 2px 5px rgba(245, 145, 145, 0.2);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #C91B1A 0%, #A01616 100%);
                color: white;
                padding: 30px 20px;
                text-align: center;
            }}
            .header h1 {{
                margin: 0;
                font-size: 24px;
                font-weight: 600;
            }}
            .content {{
                padding: 30px 20px;
            }}
            .message {{
                margin-bottom: 25px;
                font-size: 16px;
            }}
            .button-container {{
                text-align: center;
                margin: 30px 0;
            }}
            .button {{
                display: inline-block;
                background: linear-gradient(135deg, #C91B1A 0%, #A01616 100%);
                color: white;
                text-decoration: none;
                padding: 12px 30px;
                border-radius: 5px;
                font-weight: 600;
                letter-spacing: 0.5px;
            }}
            .expiry-notice {{
                background-color: #fef7f7;
                border-left: 4px solid #F59191;
                padding: 15px;
                margin: 20px 0;
                border-radius: 0 5px 5px 0;
                font-size: 14px;
                color: #8B5A5A;
            }}
            .footer {{
                background-color: #fef7f7;
                padding: 20px;
                text-align: center;
                border-top: 1px solid #F5C2C2;
            }}
            .footer p {{
                margin: 5px 0;
                color: #8B5A5A;
                font-size: 14px;
            }}
            .logo {{
                font-size: 28px;
                font-weight: bold;
                color: white;
                margin-bottom: 10px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">Caspr.</div>
                <h1>Verify Your Email Address</h1>
            </div>
            <div class="content">
                <div class="message">
                    Hello {user_name or "User"},
                </div>
                <div class="message">
                    Thank you for signing up! To complete your registration and access all features, please verify your email address by clicking the button below.
                </div>
                <div class="button-container">
                    <a href="{verification_link}" class="button" style="color: white;">Verify Email</a>
                </div>
                <div class="expiry-notice">
                    <strong>Note:</strong> This verification link will expire in {expiration_text}.
                </div>
                <div class="message">
                    If you did not create an account, please ignore this email or contact our support team if you have concerns.
                </div>
            </div>
            <div class="footer">
                <p>Best Regards,</p>
                <p>Team Caspr.</p>
                <p style="margin-top: 15px; font-size: 12px; color: #A67373;">
                    This is an automated message, please do not reply directly to this email.
                </p>
            </div>
        </div>
    </body>
    </html>
    """

    # Create message with HTML part
    message = MIMEMultipart("alternative")

    # Add plain text body
    message.attach(MIMEText(body, "plain"))

    # Add HTML body
    message.attach(MIMEText(html_body, "html"))

    # Send email using the existing send_email function
    return send_email(
        email_config=email_config,
        recipient_emails=[recipient_email],
        subject=subject,
        body=body,
        html_body=html_body,
        message=message,
    )


def send_subscribe_confirmation_email(
    subscriber_email: str,
    sender_email: str,
    sender_password: str,
    to_emails: list[str],
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
) -> bool:
    """
    Send a subscription confirmation email synchronously.

    Args:
        subscriber_email (str): Subscriber email address
        sender_email (str): Sender email address
        sender_password (str): Sender email password
        to_emails (List[str]): List of TO email addresses (cannot be empty)
        cc_emails (Optional[List[str]]): List of CC email addresses (can be empty)
        bcc_emails (Optional[List[str]]): List of BCC email addresses (can be empty)

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    # Extract subscriber name from email (e.g., abc123@example.com -> abc123)
    subscriber_name = subscriber_email.split("@")[0]

    subject = "Caspr. - New Subscribed User"

    # Plain text body
    body = f"""Dear Team,

A new user has subscribed to our newsletter.

Subscriber Details
Email: {subscriber_email}
Name: {subscriber_name}

Best regards,
Caspr. System
"""

    # HTML body with minimal styling
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #000000;
                margin: 0;
                padding: 0;
            }}
            p {{
                margin-bottom: 15px;
            }}
            .subscriber-details {{
                background-color: #f5f5f5;
                padding: 15px;
                border-left: 4px solid #0066cc;
                margin: 15px 0;
            }}
        </style>
    </head>
    <body>
        <p>Dear Team,</p>
        
        <p>A new user has subscribed to our newsletter.</p>
        
        <div class="subscriber-details">
            <p><strong>Subscriber Details</strong></p>
            <p><strong>Email:</strong> {subscriber_email}</p>
            <p><strong>Name:</strong> {subscriber_name}</p>
        </div>
                
        <p>Best regards,<br>
        Caspr. System</p>
    </body>
    </html>
    """

    # Create message
    message = MIMEMultipart("alternative")
    # Don't set headers here - let email_helper.py handle them

    # Add plain text body
    message.attach(MIMEText(body, "plain"))

    # Add HTML body
    message.attach(MIMEText(html_body, "html"))

    # Create EmailConfig with provided parameters
    email_config = EmailConfig(
        sender_email=sender_email,
        app_password=sender_password,
        cc_emails=cc_emails or [],
        bcc_emails=bcc_emails or [],
    )

    # Send email using the sync send_email function
    return send_email(
        email_config=email_config,
        recipient_emails=to_emails,
        subject=subject,
        body=body,
        html_body=html_body,
        message=message,
    )


def send_request_confirmation_email(
    client_email: str,
    client_name: str,
    client_website: str,
    description: str,
    sender_email: str,
    sender_password: str,
    to_emails: list[str],
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
) -> bool:
    """
    Send a request confirmation email synchronously.

    Args:
        client_email (str): Client email address
        client_name (str): Client name
        client_website (str): Client website (optional)
        description (str): Form brief description from the request
        sender_email (str): Sender email address
        sender_password (str): Sender email password
        to_emails (List[str]): List of TO email addresses (cannot be empty)
        cc_emails (Optional[List[str]]): List of CC email addresses (can be empty)
        bcc_emails (Optional[List[str]]): List of BCC email addresses (can be empty)

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    subject = "New Request for Caspr."

    # Plain text body
    body = f"""Dear {client_name},

Hello from Caspr.!

This is to confirm that you have submitted a request for Caspr.

Team Caspr. will reach out to you with relevant details within the hour.

Research Desk,
Please find below the details of the client request.

Form: Home page, Caspr.

Company Website: {client_website or "Not provided"}

Client Brief: {description or "Not provided"}

Best regards,
Caspr.
"""

    # HTML body with minimal styling
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #000000;
                margin: 0;
                padding: 0;
            }}
            p {{
                margin-bottom: 15px;
            }}
        </style>
    </head>
    <body>
        <p>Dear {client_name},</p>
        
        <p>Hello from Caspr.!</p>
        
        <p>This is to confirm that you have submitted a request for Caspr.</p>
        
        <p>Team Caspr. will reach out to you with relevant details within the hour.</p>
        
        <p>Research Desk,<br>
        Please find below the details of the client request.</p>
        
        <p><strong>Form:</strong> Home page, Caspr.</p>
        
        <p><strong>Company Website:</strong> {client_website or "Not provided"}</p>
        
        <p><strong>Client Brief:</strong> {description or "Not provided"}</p>
        
        <p>Best regards,<br>
        Caspr.</p>
    </body>
    </html>
    """

    # Create message
    message = MIMEMultipart("alternative")
    # Don't set headers here - let email_helper.py handle them

    # Add plain text body
    message.attach(MIMEText(body, "plain"))

    # Add HTML body
    message.attach(MIMEText(html_body, "html"))

    # Create EmailConfig with provided parameters
    email_config = EmailConfig(
        sender_email=sender_email,
        app_password=sender_password,
        cc_emails=cc_emails or [],
        bcc_emails=bcc_emails or [],
    )

    # Send email using the sync send_email function
    return send_email(
        email_config=email_config,
        recipient_emails=to_emails,
        subject=subject,
        body=body,
        html_body=html_body,
        message=message,
    )


def send_book_call_email(
    name: str,
    email: str,
    phone_country_code: str,
    phone_number: str,
    brief: str | None,
    sender_email: str,
    sender_password: str,
    to_emails: list[str],
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
) -> bool:
    """
    Send a plain text email for a call booking request.
    TO: internal team, CC: user email, BCC: from env.
    Plain text only, minimal formatting, communicative.
    """
    subject = "Call booking request"

    body = f"""A call has been requested.

Contact details:
Name: {name}
Email: {email}
Phone: {phone_country_code} {phone_number}
"""

    if brief:
        body += f"\nBrief: {brief if brief else 'Not provided'}\n"

    body += """
Please follow up as needed.
"""

    message = MIMEMultipart("alternative")
    message.attach(MIMEText(body, "plain"))

    email_config = EmailConfig(
        sender_email=sender_email,
        app_password=sender_password,
        cc_emails=cc_emails or [],
        bcc_emails=bcc_emails or [],
    )

    return send_email(
        email_config=email_config,
        recipient_emails=to_emails,
        subject=subject,
        body=body,
        message=message,
    )


def send_error_digest_email(errors: list) -> bool:
    """Send a developer digest of grouped/deduplicated 500 errors to the error alert team.

    Args:
        errors: List of error dicts produced by ErrorAlertManager.queue_error().
                Each dict has: timestamp, method, path, status_code, error_message,
                traceback, user_id, user_email, query_params.

    Returns:
        True if email sent successfully, False otherwise.
    """
    if not ERROR_ALERT_EMAILS or not ERROR_ALERT_SENDER_EMAIL or not ERROR_ALERT_SENDER_EMAIL_PASS:
        logger.warning("Error alert email config missing - skipping digest")
        return False

    if not errors:
        return True

    interval_min = ERROR_DIGEST_INTERVAL_SECONDS // 60
    dev_prefix = "DEV - " if ENVIRONMENT == "DEV" else ""
    subject = f"{dev_prefix}[CASPR ERROR DIGEST] {len(errors)} error(s) in last {interval_min} min"

    # Group by (method, path, status_code, error_message)
    groups: dict = {}
    for err in errors:
        key = (
            err.get("method", ""),
            err.get("path", ""),
            err.get("status_code", ""),
            err.get("error_message", ""),
        )
        if key not in groups:
            groups[key] = {"count": 0, "first": err}
        groups[key]["count"] += 1

    _esc = html_mod.escape
    rows_html = ""
    for (method, path, status_code, error_message), group in groups.items():
        count = group["count"]
        first = group["first"]
        traceback_snippet = (first.get("traceback") or "")[:600]
        if traceback_snippet:
            traceback_snippet = _esc(traceback_snippet)
            traceback_html = f'<pre style="font-size:11px;background:#f8f8f8;padding:6px;border-radius:4px;overflow:auto;max-width:580px">{traceback_snippet}{"..." if len(first.get("traceback", "")) > 600 else ""}</pre>'
        else:
            traceback_html = "<em style='color:#999'>None</em>"

        raw_user_id = str(first.get("user_id") or "anonymous")
        raw_user_email = str(first.get("user_email") or "")

        # Pre-auth user_ids look like "[pre-auth] amit@example.com" — pull the
        # email out so it appears in the email column too.
        if not raw_user_email and raw_user_id.startswith("[pre-auth] "):
            candidate = raw_user_id[len("[pre-auth] ") :]
            if "@" in candidate:
                raw_user_email = candidate

        user_id = _esc(raw_user_id)
        user_email = _esc(raw_user_email) if raw_user_email else "<em style='color:#999'>—</em>"
        timestamp = _esc(str(first.get("timestamp", "")))
        query_params = _esc(str(first.get("query_params") or ""))
        method_esc = _esc(str(method))
        path_esc = _esc(str(path))
        error_msg_esc = _esc(str(error_message)[:200])

        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;white-space:nowrap">{timestamp}</td>
            <td style="padding:8px;border:1px solid #ddd">
                <span style="background:#e8f0fe;color:#1a73e8;font-weight:bold;padding:2px 6px;border-radius:3px">{method_esc}</span>
            </td>
            <td style="padding:8px;border:1px solid #ddd;font-family:monospace;font-size:12px">{path_esc}{f"?{query_params}" if query_params else ""}</td>
            <td style="padding:8px;border:1px solid #ddd;text-align:center">
                <span style="background:#fce8e6;color:#d93025;font-weight:bold;padding:2px 6px;border-radius:3px">{status_code}</span>
            </td>
            <td style="padding:8px;border:1px solid #ddd;text-align:center;font-weight:bold;color:#d93025">{count}</td>
            <td style="padding:8px;border:1px solid #ddd;color:#333">{error_msg_esc}</td>
            <td style="padding:8px;border:1px solid #ddd;font-size:11px;color:#666">{user_id}</td>
            <td style="padding:8px;border:1px solid #ddd;font-size:11px;color:#1a73e8">{user_email}</td>
            <td style="padding:8px;border:1px solid #ddd">{traceback_html}</td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:1200px;margin:0 auto">
        <div style="background:#d93025;padding:16px 24px;border-radius:8px 8px 0 0">
            <h2 style="margin:0;color:#fff">Caspr Error Digest</h2>
            <p style="margin:4px 0 0;color:#fce8e6;font-size:13px">
                {len(errors)} total occurrence(s) &bull; {len(groups)} unique error(s) &bull; Last {interval_min} minutes &bull; {ENVIRONMENT}
            </p>
        </div>
        <div style="background:#fff;padding:16px 24px;border-radius:0 0 8px 8px;box-shadow:0 1px 3px rgba(0,0,0,0.12)">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <thead>
                    <tr style="background:#f5f5f5">
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">Timestamp (UTC)</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">Method</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">Endpoint</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">Status</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">Count</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">Error Message</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">User ID</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">User Email</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left">Traceback</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
        </div>
    </body></html>
    """

    plain_body = f"Caspr Error Digest - {len(errors)} occurrences, {len(groups)} unique errors in last {interval_min} min.\n\n"
    for (method, path, status_code, error_message), group in groups.items():
        plain_body += f"[x{group['count']}] {method} {path} -> {status_code}: {error_message}\n"

    try:
        email_config = EmailConfig(
            sender_email=ERROR_ALERT_SENDER_EMAIL,
            app_password=ERROR_ALERT_SENDER_EMAIL_PASS,
        )
        return send_email(
            email_config=email_config,
            recipient_emails=ERROR_ALERT_EMAILS,
            subject=subject,
            body=plain_body,
            html_body=html_body,
        )
    except Exception:
        logger.error("Failed to send error digest email", exc_info=True)
        return False


def send_payment_notification_email(event_type: str, payment_data: dict) -> bool:
    """Send a payment lifecycle notification email to the team.

    Args:
        event_type: Event identifier, e.g. TOPUP_COMPLETED, SUBSCRIPTION_CHARGED.
        payment_data: Dict with payment details (payment_id, amount, currency,
                      subscription_id, plan_tier, plan_duration, user_id, gateway, etc.)

    Returns:
        True if email sent successfully, False otherwise.
    """
    if (
        not PAYMENT_ALERT_EMAILS
        or not PAYMENT_ALERT_SENDER_EMAIL
        or not PAYMENT_ALERT_SENDER_EMAIL_PASS
    ):
        logger.warning("Payment alert email config missing - skipping notification")
        return False

    dev_prefix = "DEV - " if ENVIRONMENT == "DEV" else ""
    amount = payment_data.get("amount")
    currency = payment_data.get("currency", "")
    amount_str = f"{amount} {currency}".strip() if amount is not None else "N/A"
    subject = f"{dev_prefix}[CASPR PAYMENT] {event_type} - {amount_str}"

    # Determine badge colour based on event type
    if any(k in event_type for k in ("COMPLETED", "CHARGED", "ACTIVATED")):
        badge_bg, badge_color = "#e6f4ea", "#188038"
        header_bg = "#188038"
    elif any(k in event_type for k in ("FAILED", "HALTED", "CANCELLED", "EXPIRED")):
        badge_bg, badge_color = "#fce8e6", "#d93025"
        header_bg = "#d93025"
    elif "REFUNDED" in event_type:
        badge_bg, badge_color = "#fef7e0", "#b06000"
        header_bg = "#b06000"
    else:
        badge_bg, badge_color = "#e8f0fe", "#1a73e8"
        header_bg = "#1a73e8"

    _esc = html_mod.escape

    def _row(label: str, value) -> str:
        if value is None or value == "":
            return ""
        safe_val = _esc(str(value))
        return f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#666;font-size:13px;width:160px">{_esc(label)}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#333;font-size:13px;font-family:monospace">{safe_val}</td>
        </tr>"""

    event_type_esc = _esc(event_type)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    details_rows = (
        _row(
            "Event",
            f'<span style="background:{badge_bg};color:{badge_color};font-weight:bold;padding:2px 8px;border-radius:4px;font-family:Arial">{event_type_esc}</span>',
        )
        + _row("Timestamp", timestamp)
        + _row("Environment", ENVIRONMENT)
        + _row("Payment ID", payment_data.get("payment_id"))
        + _row("Subscription ID", payment_data.get("subscription_id"))
        + _row("User ID", payment_data.get("user_id"))
        + _row("Amount", amount_str)
        + _row("Payment Method", payment_data.get("payment_method"))
        + _row("Plan Tier", payment_data.get("plan_tier"))
        + _row("Plan Duration", payment_data.get("plan_duration"))
        + _row("Gateway", payment_data.get("gateway"))
        + _row("Status", payment_data.get("status"))
        + _row("Paid Count", payment_data.get("paid_count"))
        + _row("Refund ID", payment_data.get("refund_id"))
        + _row("Refund Amount", payment_data.get("amount_refunded_total"))
    )

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:0 auto">
        <div style="background:{header_bg};padding:16px 24px;border-radius:8px 8px 0 0">
            <h2 style="margin:0;color:#fff">Caspr Payment Notification</h2>
            <p style="margin:4px 0 0;color:rgba(255,255,255,0.85);font-size:13px">{event_type_esc} &bull; {_esc(ENVIRONMENT)}</p>
        </div>
        <div style="background:#fff;padding:16px 24px;border-radius:0 0 8px 8px;box-shadow:0 1px 3px rgba(0,0,0,0.12)">
            <table style="width:100%;border-collapse:collapse">{details_rows}</table>
        </div>
    </body></html>
    """

    plain_body = f"Caspr Payment Notification\nEvent: {event_type}\nTimestamp: {timestamp}\n"
    for k, v in payment_data.items():
        if v is not None and v != "":
            plain_body += f"{k}: {v}\n"

    try:
        email_config = EmailConfig(
            sender_email=PAYMENT_ALERT_SENDER_EMAIL,
            app_password=PAYMENT_ALERT_SENDER_EMAIL_PASS,
        )
        return send_email(
            email_config=email_config,
            recipient_emails=PAYMENT_ALERT_EMAILS,
            subject=subject,
            body=plain_body,
            html_body=html_body,
        )
    except Exception:
        logger.error(
            f"Failed to send payment notification email for event {event_type}", exc_info=True
        )
        return False
