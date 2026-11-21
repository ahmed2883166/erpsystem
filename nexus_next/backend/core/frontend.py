from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpResponse


def frontend_index(request):
    build_root = Path(settings.WHITENOISE_ROOT)
    index_file = build_root / "index.html"
    if not index_file.is_file():
        return HttpResponse(
            "واجهة React غير مبنية. شغّل npm run build داخل frontend.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    return FileResponse(index_file.open("rb"), content_type="text/html; charset=utf-8")
