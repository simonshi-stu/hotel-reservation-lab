"""Small independent, all-synthetic fixtures for concurrency experiments."""
from hotel.service import Store

def scenario_store(path, capacity=40, timeout=5.0):
    store = Store(path, timeout)
    store.initialize()
    with store.transaction(write=True) as connection:
        connection.execute("INSERT INTO hotels VALUES ('H1','SIMULATED Test Hotel','Shanghai')")
        for number, rooms in enumerate((capacity,25,12), 1):
            room = "H1-R" + str(number)
            connection.execute("INSERT INTO room_types VALUES (?, 'H1', ?, 35000, 2)", (room, "SIMULATED Type " + str(number)))
            for day in range(1, 8):
                connection.execute("INSERT INTO nightly_inventory VALUES (?,?,?,?,?)",
                                   (room, "2027-01-0" + str(day), rooms, rooms, 35000 + day*100))
        for customer in range(1, 2001):
            connection.execute("INSERT INTO customers VALUES (?,?,?,?,?)",
                               ("C" + str(customer).zfill(5), "SIMULATED Guest " + str(customer),
                                "Shanghai", "leisure", "sim-" + str(customer) + "@example.invalid"))
    return store

def request(customer=1, key="test-1", room="H1-R1", check_in="2027-01-01", check_out="2027-01-04", quantity=1):
    return {"customer_id": "C" + str(customer).zfill(5), "room_type_id": room,
            "check_in": check_in, "check_out": check_out, "quantity": quantity, "idempotency_key": key}

