# 🇪🇺 Radar EU — Licitaciones Europeas

Web estática en GitHub Pages que acumula licitaciones, concursos y open calls europeos de movilidad, transporte y formación. Búsqueda automática cada hora. Reset cada domingo a medianoche.

---

## ⚡ Setup en 5 pasos

### 1. Crear el repositorio
- Ve a github.com/new → nombre: `radar-eu` → Public → Create

### 2. Subir los archivos
```bash
git clone https://github.com/TU_USUARIO/radar-eu
cd radar-eu
# Copia todos los archivos aquí (index.html, data.json, scripts/, .github/)
git add .
git commit -m "🚀 Primer commit"
git push
```

### 3. Activar GitHub Pages
- Settings → Pages → Source: `main` / `/ (root)` → Save
- URL: `https://TU_USUARIO.github.io/radar-eu`

### 4. Añadir el secreto de Anthropic
- Settings → Secrets and variables → Actions → New repository secret
- Nombre: `ANTHROPIC_API_KEY`
- Valor: tu API key de Anthropic (`sk-ant-...`)

### 5. Lanzar la primera búsqueda
- Actions → "Búsqueda automática cada hora" → Run workflow

---

## Cómo funciona

| Qué | Cuándo |
|-----|--------|
| Búsqueda automática con IA | Cada hora (GitHub Actions + Anthropic API) |
| La web refresca los datos | Cada minuto (fetch de data.json) |
| Reset de toda la semana | Domingos a las 00:00 UTC |

**No necesitas tocar nada.** El sistema busca solo, acumula resultados sin duplicados y los muestra en tiempo real.

---

## Tecnología
- **Frontend**: HTML + CSS + JS vanilla, 0 dependencias
- **Datos**: `data.json` en el repo (GitHub Pages lo sirve como estático)
- **Motor de búsqueda**: Anthropic API `claude-sonnet-4` con web_search
- **Automatización**: GitHub Actions (cron `0 * * * *` + `0 0 * * 0`)
- **Coste**: 0 € (GitHub gratuito + Anthropic API según uso)
