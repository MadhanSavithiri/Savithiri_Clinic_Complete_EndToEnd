"""Indian-style date/time display (dd/mm/yyyy, 12-hour clock)."""
DATE_FORMAT = "d/m/Y"
SHORT_DATE_FORMAT = "d/m/Y"
DATETIME_FORMAT = "d/m/Y h:i A"
SHORT_DATETIME_FORMAT = "d/m/Y h:i A"
TIME_FORMAT = "h:i A"
DATE_INPUT_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"]
DATETIME_INPUT_FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
FIRST_DAY_OF_WEEK = 1
