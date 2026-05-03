from api.telegram_client import get_telegram_client

def send_elite_ticket(ticket_data):
    """
    Sends an elite ticket notification using the telegram client.
    """
    client = get_telegram_client()
    # Assuming ticket_data is a dictionary or an object with a __str__ representation
    message = f"Elite Ticket Notification:\n{ticket_data}"
    return client.send_message(message)

def send_startup_notification():
    """
    Sends a bot startup notification using the telegram client.
    """
    client = get_telegram_client()
    return client.send_message("Bot Started")
