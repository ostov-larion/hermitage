#!/usr/bin/env python3
"""Замер производительности «Эрмитажа» на настоящем каталоге.

    DISPLAY=:99 PYTHONPATH=src python3 tools/bench.py ~/.cache/biglib

Меряет то, что реально влияет на отзывчивость:

* индексация (скан каталога + чтение заголовков файлов);
* полный пересчёт masonry-раскладки и инкрементальную догрузку;
* поиск видимого диапазона (bisect) и шаг курсора;
* стоимость одного шага скролла (перепривязка плиток + allocation);
* декодирование миниатюр: холодный кэш / тёплый кэш на диске;
* потребление памяти процессом.
"""
from __future__ import annotations

import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from masonry_gallery.masonry import MasonryView  # noqa: E402
from masonry_gallery.model import GalleryModel  # noqa: E402
from masonry_gallery.thumbnails import ThumbnailLoader  # noqa: E402


def rss_mb() -> float:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f} мс"


def bench(target: str) -> None:
    print(f"Каталог: {target}")
    print(f"RSS на старте:         {rss_mb():.0f} МБ")
    print(f"Файлов на диске: {sum(len(f) for _, _, f in os.walk(target))}")
    print("-" * 62)

    # ---------------------------------------------------------- индексация
    model = GalleryModel()
    started = time.perf_counter()
    batches: list[float] = []
    state = {"first": None}

    loop = GLib.MainLoop()

    def on_batch(batch, done):
        if state["first"] is None:
            state["first"] = time.perf_counter() - started
        batches.append(time.perf_counter() - started)
        model.push_batch(batch)
        return False

    def on_done(count):
        model.finish_load()
        state["total"] = time.perf_counter() - started
        loop.quit()
        return False

    model.load([target], on_batch=on_batch, on_done=on_done)
    loop.run()

    print(f"индексация:            {ms(state['total'])}  ({len(model)} файлов,"
          f" {len(model) / max(state['total'], 1e-6):.0f} файлов/с)")
    print(f"первая порция в UI:    {ms(state['first'] or 0)}")
    print(f"RSS после индексации:  {rss_mb():.0f} МБ"
          f" ({(rss_mb() - 126) * 1024 * 1024 / max(1, len(model)):.0f} байт на файл)")

    # ------------------------------------------------------------ раскладка
    loader = ThumbnailLoader(memory_budget=256 << 20)
    view = MasonryView(model, loader)
    window = Adw.ApplicationWindow(default_width=1280, default_height=840)
    scroller = Gtk.ScrolledWindow()
    scroller.set_child(view)
    window.set_content(scroller)
    view.attach_adjustment(scroller.get_vadjustment())
    window.present()

    context = GLib.MainContext.default()
    deadline = time.perf_counter() + 1.0
    while time.perf_counter() < deadline:
        context.iteration(False)
        time.sleep(0.005)

    count = len(model)

    width = float(view.get_width() or 1268)
    runs = []
    for _ in range(6):
        started = time.perf_counter()
        view._compute(width, count, start=0)  # noqa: SLF001
        runs.append(time.perf_counter() - started)
    runs.sort()
    print(f"полная раскладка {count}: холодная {ms(runs[0])},"
          f" тёплая {ms(runs[len(runs) // 2])}")

    # инкрементальная догрузка: 240 элементов в конец
    started = time.perf_counter()
    view._compute(float(view.get_width() or 1268), count, start=count - 240)  # noqa: SLF001
    print(f"догрузка 240 элементов: {ms(time.perf_counter() - started)}")

    view.invalidate()
    started = time.perf_counter()
    view._last_reflow = 0.0  # noqa: SLF001 — отключаем коалесинг для честного замера
    view._ensure_layout(width)  # noqa: SLF001
    print(f"пересчёт после invalidate: {ms(time.perf_counter() - started)}")

    started = time.perf_counter()
    lookups = 0
    for _ in range(200):
        view._visible(0.0, 2000.0)  # noqa: SLF001
        lookups += 1
    print(f"поиск видимых, 200 раз: {ms(time.perf_counter() - started)}")

    view.select_path(model[0].path, scroll=False)
    started = time.perf_counter()
    for _ in range(200):
        view.move_cursor(0, 1)
    print(f"шаг курсора, 200 раз:  {ms(time.perf_counter() - started)}")

    # ------------------------------------------------- стоимость скролла
    adjustment = scroller.get_vadjustment()
    total_height = view._height  # noqa: SLF001
    steps = 60
    frame_times: list[float] = []
    for step in range(steps):
        position = (total_height - 900) * step / steps
        adjustment.set_value(position)
        started = time.perf_counter()
        view._update()  # noqa: SLF001 — ровно то, что происходит на кадр
        frame_times.append(time.perf_counter() - started)
        context.iteration(False)

    frame_times.sort()
    print(f"шаг скролла: медиана {ms(frame_times[len(frame_times) // 2])},"
          f" p95 {ms(frame_times[int(len(frame_times) * 0.95)])},"
          f" макс {ms(frame_times[-1])}")
    print(f"живых плиток: {len(view._active)}, в пуле: {len(view._pool)},"  # noqa: SLF001
          f" всего виджетов-плиток: {len(view._active) + len(view._pool)}")  # noqa: SLF001

    # ---------------------------------------------------------- миниатюры
    import tempfile

    sample = [model[i * max(1, count // 24)] for i in range(24)]
    sample = sample[:24]

    def measure(loader_, items):
        """Ждёт доставки всех миниатюр и возвращает время на штуку."""
        remaining = {"n": len(items)}
        finished = GLib.MainLoop()

        def on_thumb(_path, _texture):
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                finished.quit()

        started_ = time.perf_counter()
        for item in items:
            loader_.request(item, 256, on_thumb)
        if remaining["n"] > 0:  # попадания в RAM-кэш приходят синхронно
            GLib.timeout_add(20000, finished.quit)
            finished.run()
        return (time.perf_counter() - started_) / max(1, len(items))

    with tempfile.TemporaryDirectory() as cache_dir:
        cold_loader = ThumbnailLoader(cache_dir=cache_dir)
        cold = measure(cold_loader, sample)
        print(f"миниатюра, пустой кэш: {ms(cold)} на файл"
              f" ({1 / max(cold, 1e-9):.0f} шт/с), декодировано {cold_loader.stats['decoded']}")

        warm = measure(cold_loader, sample)
        print(f"миниатюра, RAM-кэш:    {ms(warm)} на файл"
              f" (попаданий {cold_loader.stats['hits']})")

        disk_loader = ThumbnailLoader(cache_dir=cache_dir)
        disk = measure(disk_loader, sample)
        print(f"миниатюра, кэш диска:  {ms(disk)} на файл"
              f" (из кэша {disk_loader.stats['disk']})")
        cold_loader.shutdown()
        disk_loader.shutdown()

    print(f"RAM-кэш текстур: {loader.cache_size_bytes() / (1 << 20):.1f} МБ")
    print(f"RSS итого:             {rss_mb():.0f} МБ")
    print("-" * 62)

    loader.shutdown()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.cache/biglib")
    app = Adw.Application(application_id="io.github.hermitage.Bench")
    app.register(None)

    def run_bench(*_args):
        bench(target)
        app.quit()

    GLib.idle_add(run_bench)
    app.run([])
