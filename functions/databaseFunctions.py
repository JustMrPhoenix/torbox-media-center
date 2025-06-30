from tinydb import TinyDB
import threading

# Global lock for thread safety
_db_lock = threading.Lock()

def getDatabase(name: str = "db"):
    """
    Returns the TinyDB database instance.
    """
    try:
        return TinyDB(f"{name}.json")
    except Exception as e:
        print(f"Error connecting to the database: {e}")
        return None

def clearDatabase(type: str):
    """
    Clears the entire database.
    """
    db = getDatabase(type)
    if db is None:
        return False, "Database connection failed."
    try:
        with _db_lock:
            db.truncate()
        return True, "Database cleared successfully."
    except Exception as e:
        return False, f"Error clearing the database: {e}"
    
def insertData(data: dict, type: str):
    """
    Inserts data into the database.
    """
    db = getDatabase(type)
    if db is None:
        return False, "Database connection failed."
    try:
        with _db_lock:
            db.insert(data)
        return True, "Data inserted successfully."
    except Exception as e:
        return False, f"Error inserting data. {e}"
    
def getAllData(type: str):
    """
    Retrieves all data from the database.
    """
    db = getDatabase(type)
    if db is None:
        return None, False, "Database connection failed."
    try:
        with _db_lock:
            data = db.all()
        return data, True, "Data retrieved successfully."
    except Exception as e:
        return None, False, f"Error retrieving data. {e}"