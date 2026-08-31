# Tu libreta de conocimiento

Todo archivo `.md` o `.txt` que pongas en esta carpeta pasa a ser parte de lo
que tu agente sabe de tu negocio. Con `FEATURE_RAG=on`, cada vez que Railway
despliega tu copia, el agente lee estos archivos, los trocea y los tiene a la
mano para responder con TUS datos.

Los tres archivos de ejemplo describen un negocio inventado, «Panadería La
Espiga», para que veas el formato y pruebes desde el primer día. Bórralos
cuando subas los tuyos.

## Qué meter

- Catálogo con precios y descripciones cortas.
- Horarios, ubicación, formas de pago y de entrega.
- Preguntas frecuentes con su respuesta tal como te gusta darla.
- Políticas: cambios, devoluciones, tiempos de entrega, anticipos.

## Cómo prepararlo

- Un tema por archivo, con títulos (`#`, `##`) y párrafos cortos: el agente
  subraya por párrafos, así que cada párrafo debería poder leerse solo.
- Escribe como le hablarías a un cliente; el agente copia tu tono.
- Sin datos sensibles: nada de contraseñas, datos bancarios completos ni
  información personal de clientes.

## Cómo probarlo

Escríbele a tu agente una pregunta cuya respuesta esté en un archivo. Si no la
encuentra, revisa que la pregunta y el documento compartan palabras clave
(«horario», «envío», el nombre del producto) y que el archivo tenga extensión
`.md` o `.txt`.
