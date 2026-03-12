import os
import time
from datetime import datetime, timedelta

# Set the reminder time
reminder_time = "19:00"

# Get the current date
current_date = datetime.now().date()

# Calculate the next reminder date
next_reminder = current_date if current_date.strftime("%H:%M") <= reminder_time else current_date + timedelta(days=1)

# Set the reminder time
next_reminder_time = datetime.combine(next_reminder, datetime.strptime(reminder_time, "%H:%M").time())

# Get the path to the reminders file
reminders_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'reminders.txt')

# Append the reminder to the reminders file
with open(reminders_file, 'a') as f:
    f.write(f"Take medicine at {reminder_time} on {next_reminder.strftime('%Y-%m-%d')}\n")

# Print confirmation
print("Reminder set for daily medicine at 7 p.m.")