import pygame
import time

pygame.init()
pygame.joystick.init()

if pygame.joystick.get_count() == 0:
    raise SystemExit("No controller found")

js = pygame.joystick.Joystick(0)
js.init()

print(f"Device: {js.get_name()}")
print(f"Buttons: {js.get_numbuttons()}")
print(f"Axes: {js.get_numaxes()}")
print(f"Hats: {js.get_numhats()}")

controllers = {
    "Controller 1": {
        "buttons": {
            1: "Y",
            3: "B",
            5: "A",
            7: "X",
            9: "L1",
            11: "R1",
            13: "L2",
            15: "R2",
            17: "SELECT",
            19: "START",
            21: "L3",
            23: "R3",
        },
        "axes": {
            "LX": 1,
            "LY": 3,
            "RX": 7,
            "RY": 5,
        },
        "hat": 1,
    },

    "Controller 2": {
        "buttons": {
            0: "Y",
            2: "B",
            4: "A",
            6: "X",
            8: "L1",
            10: "R1",
            12: "L2",
            14: "R2",
            16: "SELECT",
            18: "START",
            20: "L3",
            22: "R3",
        },
        "axes": {
            "LX": 0,
            "LY": 2,
            "RX": 6,
            "RY": 4,
        },
        "hat": 0,
    }
}


last_buttons = {}
last_axes = {}
last_hats = {}


def axis_filter(v):
    if abs(v) < 0.15:
        return 0
    return round(v, 2)


print("\nListening...\n")


while True:

    pygame.event.pump()

    for player, cfg in controllers.items():

        # Buttons
        for button, name in cfg["buttons"].items():

            state = js.get_button(button)

            old = last_buttons.get(button, 0)

            if state != old:
                print(
                    f"{player} | {name}: "
                    f"{'PRESSED' if state else 'RELEASED'}"
                )
                last_buttons[button] = state


        # Hats
        hat_id = cfg["hat"]

        if hat_id < js.get_numhats():

            hat = js.get_hat(hat_id)

            old = last_hats.get(hat_id, (0,0))

            if hat != old:

                directions = {
                    (0,1): "UP",
                    (0,-1): "DOWN",
                    (-1,0): "LEFT",
                    (1,0): "RIGHT",
                    (0,0): "RELEASED"
                }

                print(
                    f"{player} | D-PAD: "
                    f"{directions.get(hat, hat)}"
                )

                last_hats[hat_id] = hat


        # Axes
        for name, axis in cfg["axes"].items():

            value = axis_filter(js.get_axis(axis))

            old = last_axes.get(axis, 0)

            if value != old:
                print(
                    f"{player} | {name}: {value:+.2f}"
                )

                last_axes[axis] = value


    time.sleep(0.01)
