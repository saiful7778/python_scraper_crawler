from worker import worker

@worker
async def send_welcome_email(email: str, name: str) -> dict:
    return {"status": "sent", "email": email, "name": name}
