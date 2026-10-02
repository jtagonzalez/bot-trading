import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
"""
╔══════════════════════════════════════════════════════╗
║     Test de Conexión a IQ Option                     ║
║     Ejecuta este script para verificar que todo      ║
║     está configurado correctamente.                  ║
╚══════════════════════════════════════════════════════╝

Uso:
  python test_conexion.py
"""

from iqoptionapi.stable_api import IQ_Option
import time

print("=" * 50)
print("  TEST DE CONEXIÓN — IQ Option API")
print("=" * 50)

# ── Paso 1: Ingresar credenciales ──
print("\n📧 Ingresa tus datos de IQ Option:\n")
email = input("   Email: ").strip()
password = input("   Contraseña: ").strip()

# ── Paso 2: Intentar conexión ──
print("\n🔌 Conectando...")
api = IQ_Option(email, password)
status, reason = api.connect()

# ── Paso 3: Manejar 2FA si es necesario ──
if not status and reason == "2FA":
    print("\n🔐 Verificación en dos pasos (2FA) detectada")
    print("   IQ Option envió un código SMS a tu teléfono.\n")
    codigo = input("   Ingresa el código SMS: ").strip()
    status, reason = api.connect_2fa(codigo)

# ── Paso 4: Verificar resultado ──
if status:
    print("\n✅ ¡CONEXIÓN EXITOSA!")

    # Mostrar info de la cuenta
    api.change_balance("PRACTICE")
    balance_demo = api.get_balance()

    api.change_balance("REAL")
    balance_real = api.get_balance()

    print(f"\n   📊 Balance DEMO:  ${balance_demo:.2f}")
    print(f"   💰 Balance REAL:  ${balance_real:.2f}")

    # Verificar activos disponibles
    print("\n   🔍 Verificando activos disponibles...")
    api.change_balance("PRACTICE")

    try:
        todos = api.get_all_open_time()
        activos_binarios = 0
        activos_abiertos = 0

        if "binary" in todos:
            for activo, info in todos["binary"].items():
                activos_binarios += 1
                if info.get("open"):
                    activos_abiertos += 1

        print(f"   📋 Activos binarios totales: {activos_binarios}")
        print(f"   🟢 Activos abiertos ahora:   {activos_abiertos}")

    except Exception as e:
        print(f"   ⚠️ No se pudieron listar activos: {e}")

    # Probar obtener velas
    print("\n   📊 Probando obtención de velas (EURUSD)...")
    try:
        velas = api.get_candles("EURUSD", 60, 5, time.time())
        if velas:
            print(f"   ✅ Se obtuvieron {len(velas)} velas correctamente")
            ultima = velas[-1]
            print(f"      Última vela: Open={ultima['open']:.5f} "
                  f"Close={ultima['close']:.5f}")
        else:
            print("   ⚠️ EURUSD puede estar cerrado. Probando EURUSD-OTC...")
            velas = api.get_candles("EURUSD-OTC", 60, 5, time.time())
            if velas:
                print(f"   ✅ Se obtuvieron {len(velas)} velas de EURUSD-OTC")
    except Exception as e:
        print(f"   ⚠️ Error obteniendo velas: {e}")

    print("\n" + "=" * 50)
    print("  ✅ TODO LISTO — Puedes ejecutar el bot:")
    print("     python gold_bot_v3.py")
    print("=" * 50)

else:
    print(f"\n❌ ERROR DE CONEXIÓN")
    print(f"   Razón: {reason}\n")

    if "invalid_credentials" in str(reason):
        print("   🔧 SOLUCIÓN: Email o contraseña incorrectos.")
        print("      • Verifica que el email es el de tu cuenta IQ Option")
        print("      • Verifica que la contraseña es correcta")
        print("      • Si usas login con Google/Facebook, necesitas")
        print("        crear una contraseña en IQ Option primero")

    elif "2FA" in str(reason):
        print("   🔧 SOLUCIÓN: La verificación 2FA falló.")
        print("      • Asegúrate de ingresar el código SMS correcto")
        print("      • El código expira rápido, intenta de nuevo")

    else:
        print("   🔧 POSIBLES SOLUCIONES:")
        print("      1. Cierra IQ Option en el navegador")
        print("      2. Verifica tu conexión a internet")
        print("      3. Instala websocket correcto:")
        print("         pip install websocket-client==0.56")
        print("      4. Reinstala la API:")
        print("         pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git")

    print(f"\n   Si el problema persiste, ejecuta con debug:")
    print(f"   python -c \"")
    print(f"   import logging")
    print(f"   logging.basicConfig(level=logging.DEBUG)")
    print(f"   from iqoptionapi.stable_api import IQ_Option")
    print(f"   api = IQ_Option('{email}', '***')")
    print(f"   print(api.connect())\"")
