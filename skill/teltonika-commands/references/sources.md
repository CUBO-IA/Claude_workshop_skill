# Fuentes y alcance documental

## Fuente oficial principal

- **Teltonika Telematics Wiki — FMB SMS/GPRS Commands:** https://wiki.teltonika-gps.com/view/FMB_SMS/GPRS_Commands

El catálogo es una **selección de 16 comandos**, no una transcripción exhaustiva. Los nombres de comando, parámetros y formatos son identificadores técnicos que se conservan sin traducir. Las descripciones y recomendaciones de uso están escritas en español.

## Aspectos por verificar por modelo y firmware

- Disponibilidad real de cada comando en el modelo y versión de firmware concreta.
- Formato exacto de sus argumentos y parámetros, especialmente `setparam`, `getparam`, `flush` y `setdigout`.
- Configuración de usuario y contraseña SMS y efecto de los modos de ahorro de energía.
- Respuesta esperada y condiciones operativas de cada comando.

## Fuente de integración 1GLOBAL

- La API de 1GLOBAL y las herramientas MCP **no forman parte del catálogo oficial de comandos Teltonika**.
- El MCP propuesto `send_sms` / `get_sms_status` / `get_sms_response` constituye una **interfaz de diseño** que deberá confrontarse con la implementación real.
- El proyecto independiente debe verificar la documentación y contrato de API 1GLOBAL vigentes, las credenciales autorizadas, la política de envío y la correlación de SMS entrantes con SMS salientes.
- El archivo de descripción de Tachikoma aportado por el usuario sirve como **referencia de arquitectura**, pero no demuestra la existencia de un MCP instalado en este laboratorio.

## Criterios de verificación

1. Usar documentación oficial vigente, con URL concreta.
2. No confundir cobertura por familia con compatibilidad confirmada de firmware.
3. No inventar parámetros, respuestas de dispositivos ni confirmaciones de entrega.
4. Documentar explícitamente incertidumbre y condiciones no comprobadas.
