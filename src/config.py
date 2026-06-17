import os

__version__ = os.getenv("API_TAG_VERSION", "0.1.0")
__project_id__ = os.getenv("PROJECT_ID", "RGMC0001")

BC_CLIENT_ID = os.getenv("BC_CLIENT_ID", "")
BC_CLIENT_SECRET = os.getenv("BC_CLIENT_SECRET", "")
BC_TENANT_ID = os.getenv("BC_TENANT_ID", "")
BC_SCOPE = os.getenv("BC_SCOPE", "https://api.businesscentral.dynamics.com/.default")
BC_AUTH_URL = os.getenv("BC_AUTH_URL", f"https://login.microsoftonline.com/{os.getenv('BC_TENANT_ID', '')}/oauth2/v2.0/token")

BC_ENVIRONMENT = os.getenv("BC_ENVIRONMENT", "UAT")

revision_code = os.environ.get("K_REVISION", "00001")

# Error notification email (leave blank to disable)
developer_email = os.getenv("DEVELOPER_EMAIL", "")
smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
smtp_port = int(os.getenv("SMTP_PORT", "587"))
smtp_user = os.getenv("SMTP_USER", "")
smtp_password = os.getenv("SMTP_PASSWORD", "")

# Legacy email settings (used by send_mail.send_mail)
mail_recipient = os.getenv("MAIL_RECIPIENT", "")
mail_sender = os.getenv("MAIL_SENDER", "")
mail_password = os.getenv("MAIL_PASSWORD", "")
mail_port = int(os.getenv("MAIL_PORT", "587"))
mail_server = os.getenv("MAIL_SERVER", "smtp.gmail.com")
