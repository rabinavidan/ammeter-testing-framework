import threading
import time

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.client import request_current_from_ammeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter

# Ports and commands as documented in README.md
GREENLEE_PORT = 5000
ENTES_PORT = 5001
CIRCUTOR_PORT = 5002


def run_greenlee_emulator(error_simulator=None):
    greenlee = GreenleeAmmeter(GREENLEE_PORT, error_simulator)
    greenlee.start_server()

def run_entes_emulator(error_simulator=None):
    entes = EntesAmmeter(ENTES_PORT, error_simulator)
    entes.start_server()

def run_circutor_emulator(error_simulator=None):
    circutor = CircutorAmmeter(CIRCUTOR_PORT, error_simulator)
    circutor.start_server()

if __name__ == "__main__":
    # Start each ammeter in a separate thread
    threading.Thread(target=run_greenlee_emulator, daemon=True).start()
    threading.Thread(target=run_entes_emulator, daemon=True).start()
    threading.Thread(target=run_circutor_emulator, daemon=True).start()

    # Wait for the servers to start, if you have problem restarting the servers between runs try increasing sleep time.
    time.sleep(5)
    request_current_from_ammeter(GREENLEE_PORT, b'MEASURE_GREENLEE -get_measurement')  # Request from Greenlee Ammeter
    request_current_from_ammeter(ENTES_PORT, b'MEASURE_ENTES -get_data')  # Request from ENTES Ammeter
    request_current_from_ammeter(CIRCUTOR_PORT, b'MEASURE_CIRCUTOR -get_measurement')  # Request from CIRCUTOR Ammeter
