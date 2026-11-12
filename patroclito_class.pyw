import time
import queue
import threading
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
# OJO: pywinauto/comtypes NO se importan aquí. Al importarse inicializan COM en
# modo MTA en el hilo de Tk y el diálogo "Elegir carpeta" (STA) se congela.
# Se importan dentro del hilo de captura (ver _bucle_captura).

# Textos propios de la interfaz de Windows que debemos ignorar
UI_NOISE = {
    "Subtítulos en directo",
    "Live Captions",
    "Listo para mostrar subtítulos en directo",
    "Listo para mostrar subtítulos",
    "Ready to caption",
    "Configuración",
    "Settings"
}

def extraer_texto_limpio(ventana) -> str:
    """Extrae el texto del contenedor de subtítulos filtrando botones de la UI."""
    bloques = []
    for elem in ventana.descendants(control_type="Text"):
        txt = elem.window_text().strip()
        if txt and txt not in UI_NOISE:
            bloques.append(txt)
    return " ".join(bloques).strip()

def calcular_delta_con_numeros(acumulado: str, actual: str) -> str:
    """
    Compara por lista de palabras/números (tokens) en lugar de caracteres sueltos.
    Garantiza que números cortos (ej. '1', '5', '10%') no sean descartados.
    """
    if not acumulado:
        return actual
    if acumulado == actual:
        return ""

    palabras_acum = acumulado.split()
    palabras_act = actual.split()

    if not palabras_act:
        return ""

    # Si todo el bloque actual ya está exactamente al final del acumulado, no hay nada nuevo
    n_act = len(palabras_act)
    if len(palabras_acum) >= n_act and palabras_acum[-n_act:] == palabras_act:
        return ""

    # Buscar solapamiento exacto de palabras/números desde 1 token en adelante
    max_k = min(len(palabras_acum), len(palabras_act))
    for k in range(max_k, 0, -1):
        if palabras_acum[-k:] == palabras_act[:k]:
            nuevas = palabras_act[k:]
            return " ".join(nuevas)

    # Si la última palabra está siendo completada en vivo o es una línea nueva
    if actual.startswith(acumulado):
        return actual[len(acumulado):].strip()

    return actual


class ArgonCaptionsApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Patroclito_class - Live Captions Recorder")
        self.root.geometry("620x520")
        self.root.configure(bg="#1e1e2e")
        self.root.resizable(True, True)

        self.grabando = False
        self.hilo_captura = None
        self.hora_parada = None
        self.carpeta_destino = Path.home() / "Desktop"
        self.archivo_actual = None
        self.cola_log = queue.Queue()

        self._construir_interfaz()
        self.root.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        self._vaciar_cola_log()

    def _construir_interfaz(self):
        estilo = ttk.Style()
        estilo.theme_use("clam")
        estilo.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4", font=("Segoe UI", 10))
        estilo.configure("Header.TLabel", font=("Segoe UI", 12, "bold"), foreground="#89b4fa")
        estilo.configure("Status.TLabel", font=("Segoe UI", 9, "bold"), foreground="#a6e3a1")

        main_frame = tk.Frame(self.root, bg="#1e1e2e", padx=15, pady=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Título y estado
        top_frame = tk.Frame(main_frame, bg="#1e1e2e")
        top_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(top_frame, text="Patroclito_class | Live Captions (Win + Ctrl + L)", style="Header.TLabel").pack(side=tk.LEFT)
        self.lbl_estado = ttk.Label(top_frame, text="● Detenido", foreground="#f38ba8", font=("Segoe UI", 10, "bold"))
        self.lbl_estado.pack(side=tk.RIGHT)

        # Selección de carpeta y nombre
        file_frame = tk.LabelFrame(main_frame, text=" Archivo de Salida ", bg="#1e1e2e", fg="#89b4fa", font=("Segoe UI", 9, "bold"), padx=10, pady=8)
        file_frame.pack(fill=tk.X, pady=5)

        tk.Label(file_frame, text="Nombre de clase:", bg="#1e1e2e", fg="#cdd6f4").grid(row=0, column=0, sticky="w")
        self.entry_nombre = tk.Entry(file_frame, bg="#313244", fg="#ffffff", insertbackground="white", relief=tk.FLAT, width=28)
        self.entry_nombre.insert(0, "clase_universidad")
        self.entry_nombre.grid(row=0, column=1, padx=8, pady=3, sticky="we")

        btn_carpeta = tk.Button(file_frame, text="Cambiar Carpeta", command=self.seleccionar_carpeta, bg="#45475a", fg="white", relief=tk.FLAT, padx=8)
        btn_carpeta.grid(row=0, column=2, padx=4)

        self.lbl_ruta = tk.Label(file_frame, text=f"Destino: {self.carpeta_destino}", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI", 8))
        self.lbl_ruta.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        file_frame.columnconfigure(1, weight=1)

        # Configuración de parada automática
        stop_frame = tk.LabelFrame(main_frame, text=" Cuándo detener la grabación ", bg="#1e1e2e", fg="#89b4fa", font=("Segoe UI", 9, "bold"), padx=10, pady=8)
        stop_frame.pack(fill=tk.X, pady=5)

        self.modo_parada = tk.StringVar(value="manual")

        rb_manual = tk.Radiobutton(stop_frame, text="Manual (con el botón Detener)", variable=self.modo_parada, value="manual", bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244", activebackground="#1e1e2e")
        rb_manual.grid(row=0, column=0, columnspan=2, sticky="w")

        rb_timer = tk.Radiobutton(stop_frame, text="Por duración (minutos):", variable=self.modo_parada, value="minutos", bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244", activebackground="#1e1e2e")
        rb_timer.grid(row=1, column=0, sticky="w", pady=3)
        self.spin_minutos = tk.Spinbox(stop_frame, from_=1, to=480, width=8, bg="#313244", fg="white", relief=tk.FLAT)
        self.spin_minutos.delete(0, tk.END)
        self.spin_minutos.insert(0, "90")
        self.spin_minutos.grid(row=1, column=1, sticky="w", padx=8)

        rb_hora = tk.Radiobutton(stop_frame, text="A una hora exacta (HH:MM):", variable=self.modo_parada, value="hora", bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244", activebackground="#1e1e2e")
        rb_hora.grid(row=2, column=0, sticky="w", pady=3)
        self.entry_hora = tk.Entry(stop_frame, width=10, bg="#313244", fg="white", insertbackground="white", relief=tk.FLAT)
        hora_sugerida = (datetime.now() + timedelta(hours=1, minutes=30)).strftime("%H:%M")
        self.entry_hora.insert(0, hora_sugerida)
        self.entry_hora.grid(row=2, column=1, sticky="w", padx=8)

        self.lbl_cuenta_regresiva = tk.Label(stop_frame, text="Tiempo restante: --:--:--", bg="#1e1e2e", fg="#f9e2af", font=("Consolas", 10, "bold"))
        self.lbl_cuenta_regresiva.grid(row=1, column=2, rowspan=2, padx=20, sticky="e")

        # Botones de control
        btn_frame = tk.Frame(main_frame, bg="#1e1e2e")
        btn_frame.pack(fill=tk.X, pady=8)

        self.btn_iniciar = tk.Button(btn_frame, text="▶ INICIAR GRABACIÓN", command=self.iniciar_grabacion, bg="#a6e3a1", fg="#11111b", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, pady=6)
        self.btn_iniciar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        self.btn_detener = tk.Button(btn_frame, text="■ DETENER Y GUARDAR", command=self.detener_grabacion, bg="#f38ba8", fg="#11111b", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, pady=6, state=tk.DISABLED)
        self.btn_detener.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(5, 0))

        # Visor de transcripción en vivo (soporta números y símbolos)
        ttk.Label(main_frame, text="Vista previa en vivo (incluye números y cifras):").pack(anchor="w", pady=(4, 2))
        self.txt_log = tk.Text(main_frame, bg="#11111b", fg="#cdd6f4", font=("Consolas", 10), relief=tk.FLAT, wrap=tk.WORD, height=10)
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    def seleccionar_carpeta(self):
        inicial = self.carpeta_destino if Path(self.carpeta_destino).exists() else Path.home()
        carpeta = filedialog.askdirectory(parent=self.root, initialdir=str(inicial), mustexist=True,
                                          title="Elegir carpeta donde guardar las transcripciones")
        if carpeta:
            self.carpeta_destino = Path(carpeta)
            self.lbl_ruta.config(text=f"Destino: {self.carpeta_destino}")

    def calcular_hora_parada(self):
        modo = self.modo_parada.get()
        ahora = datetime.now()
        if modo == "minutos":
            try:
                mins = int(self.spin_minutos.get())
                return ahora + timedelta(minutes=mins)
            except ValueError:
                messagebox.showerror("Error", "Ingresa un número válido de minutos.")
                return False
        elif modo == "hora":
            try:
                partes = self.entry_hora.get().strip().split(":")
                h, m = int(partes[0]), int(partes[1])
                objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
                if objetivo <= ahora:
                    objetivo += timedelta(days=1)
                return objetivo
            except Exception:
                messagebox.showerror("Error", "Formato de hora inválido. Usa HH:MM (ej. 18:45).")
                return False
        return None

    def iniciar_grabacion(self):
        parada = self.calcular_hora_parada()
        if parada is False:
            return
        self.hora_parada = parada

        nombre_base = self.entry_nombre.get().strip() or "clase"
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.archivo_actual = self.carpeta_destino / f"{nombre_base}_{ts}.txt"

        with open(self.archivo_actual, "a", encoding="utf-8") as f:
            f.write(f"=== SESIÓN: {nombre_base.upper()} ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===\n\n")

        self.grabando = True
        self.btn_iniciar.config(state=tk.DISABLED)
        self.btn_detener.config(state=tk.NORMAL)
        self.lbl_estado.config(text="● Grabando en vivo", foreground="#a6e3a1")
        self._agregar_log(f"[*] Grabando en: {self.archivo_actual.name}\n")

        self.hilo_captura = threading.Thread(target=self._bucle_captura, daemon=True)
        self.hilo_captura.start()
        self._actualizar_reloj()

    def detener_grabacion(self):
        if not self.grabando:
            return
        self.grabando = False
        self.btn_iniciar.config(state=tk.NORMAL)
        self.btn_detener.config(state=tk.DISABLED)
        self.lbl_estado.config(text="● Guardado y Detenido", foreground="#f9e2af")
        self.lbl_cuenta_regresiva.config(text="Tiempo restante: Finalizado")
        self._agregar_log(f"\n[OK] Transcripción cerrada en: {self.archivo_actual}\n")

    def _actualizar_reloj(self):
        if not self.grabando:
            return
        if self.hora_parada:
            restante = self.hora_parada - datetime.now()
            segundos_totales = int(restante.total_seconds())
            if segundos_totales <= 0:
                self._agregar_log("\n[!] Tiempo programado alcanzado. Deteniendo grabación...")
                self.detener_grabacion()
                return
            hrs, rem = divmod(segundos_totales, 3600)
            mins, segs = divmod(rem, 60)
            self.lbl_cuenta_regresiva.config(text=f"Parada en: {hrs:02d}:{mins:02d}:{segs:02d}")
        else:
            self.lbl_cuenta_regresiva.config(text="Modo: Parada Manual")
        self.root.after(1000, self._actualizar_reloj)

    def _agregar_log(self, texto: str):
        self.txt_log.insert(tk.END, texto)
        self.txt_log.see(tk.END)

    def _vaciar_cola_log(self):
        """Los hilos solo ponen texto en la cola; únicamente el hilo de Tk toca la UI."""
        try:
            while True:
                self._agregar_log(self.cola_log.get_nowait())
        except queue.Empty:
            pass
        self.root.after(250, self._vaciar_cola_log)

    def _al_cerrar(self):
        self.grabando = False
        if self.hilo_captura and self.hilo_captura.is_alive():
            self.hilo_captura.join(timeout=3)  # deja que el hilo guarde el buffer pendiente
        self.root.destroy()

    def _guardar_fragmento(self, texto: str):
        if not texto.strip():
            return
        hora = datetime.now().strftime("%H:%M:%S")
        linea = f"[{hora}] {texto.strip()}\n"
        with open(self.archivo_actual, "a", encoding="utf-8") as f:
            f.write(linea)
        self.cola_log.put(linea)

    def _bucle_captura(self):
        # Import tardío: COM (MTA) queda confinado a este hilo, no al de Tk.
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
        except Exception as e:
            self.cola_log.put(f"[ERROR] No se pudo iniciar UI Automation: {e}\n")
            return
        historial_reciente = ""
        ultimo_texto_visto = ""
        ultimo_cambio_ts = time.time()
        buffer_pendiente = ""

        while self.grabando:
            try:
                ventanas = desktop.windows(class_name="LiveCaptionsDesktopWindow")
                if not ventanas:
                    ventanas = desktop.windows(title_re=".*Subtítulos en directo.*|.*Live Captions.*")

                if not ventanas:
                    time.sleep(0.8)
                    continue

                texto_actual = extraer_texto_limpio(ventanas[0])

                if texto_actual and texto_actual != ultimo_texto_visto:
                    delta = calcular_delta_con_numeros(historial_reciente, texto_actual)
                    if delta:
                        buffer_pendiente += (" " + delta if buffer_pendiente else delta)
                        # Ventana deslizante amplia que preserva dígitos y palabras
                        historial_reciente = (historial_reciente + " " + delta).strip()[-1200:]
                        ultimo_cambio_ts = time.time()
                    ultimo_texto_visto = texto_actual

                tiempo_sin_hablar = time.time() - ultimo_cambio_ts
                if buffer_pendiente and (tiempo_sin_hablar >= 1.5 or len(buffer_pendiente) >= 220):
                    self._guardar_fragmento(buffer_pendiente)
                    buffer_pendiente = ""

            except Exception:
                pass

            time.sleep(0.35)

        # Al detener, vaciar cualquier texto o número que haya quedado en el buffer
        if buffer_pendiente.strip():
            self._guardar_fragmento(buffer_pendiente)


if __name__ == "__main__":
    root = tk.Tk()
    app = ArgonCaptionsApp(root)
    root.mainloop()