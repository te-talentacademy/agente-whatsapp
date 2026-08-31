# Los ojos: entender las fotos que te mandan. Se enciende con FEATURE_VISION=on
# y solo trabaja cuando hay una imagen adjunta — sin foto no se invoca ni gasta.
#   router.py -> descarga las fotos del turno (con tope) y las prepara para el
#                segundo motor (OPENROUTER_VISION_MODEL).
