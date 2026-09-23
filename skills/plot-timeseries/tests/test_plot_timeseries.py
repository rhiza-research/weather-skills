"""Correctness tests for plot-timeseries."""
import json
from pathlib import Path
import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_figure_history

@pytest.fixture(scope='module')
def plot_timeseries():
    return load_skill('plot-timeseries', 'plot_timeseries').plot_timeseries

def test_single_input_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_fontsize_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}],"theme":{"fontsize":22},"title":"Large labels"}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_parse_figsize():
    import argparse
    plot_mod = load_skill('plot-timeseries', 'plot_timeseries')
    assert plot_mod.parse_figsize('10,6') == (10.0, 6.0)
    assert plot_mod.parse_figsize('8x5') == (8.0, 5.0)
    with pytest.raises(argparse.ArgumentTypeError, match='W,H'):
        plot_mod.parse_figsize('wide')

def test_figsize_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}],"layout":{"figsize":[9.0,4.0]}}')
    assert Path(out).exists()
    import matplotlib.image as mpimg
    img = mpimg.imread(out)
    assert img.shape[1] == 9 * 150
    assert img.shape[0] == 4 * 150
    history = load_figure_history(out)
    assert history[-1]['args']['spec']['layout']['figsize'] == [9.0, 4.0]

def test_reduce_spatial_dims(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}],"title":"Area mean"}')
    assert Path(out).exists()

def test_forecast_step_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast()
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'fc.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_subplots_two_inputs_write_png(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / 'a.zarr')
    b = write_zarr(make_gridded(fill=2.0), tmp_path / 'b.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(a), '-i', str(b), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}],"layout":{"facet":{"per_trace":true}},"inputs":[{"label":"A"},{"label":"B"}],"title":"Two panels"}')
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]['args']['spec']['layout']['facet']['per_trace'] is True
    assert history[-1]['args']['spec']['title'] == 'Two panels'

def test_two_inputs_write_png(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / 'a.zarr')
    b = write_zarr(make_gridded(fill=2.0), tmp_path / 'b.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(a), '-i', str(b), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_repeated_dash_i_keeps_every_input(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(name='other'), tmp_path / 'a.zarr')
    b = write_zarr(make_gridded(), tmp_path / 'b.zarr')
    with pytest.raises(SystemExit) as exc:
        run_skill(plot_timeseries, '-i', str(a), '-i', str(b), '-o', str(tmp_path / 'ts.png'), '--spec', '{"inputs":[{"variable":"precip"}],"traces":[{"reduce":["latitude","longitude"]}]}')
    assert exc.value.code == 2

def test_y_label_shows_units_from_pint():
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    da = make_gridded()['precip']
    assert mod._y_label('precip', da) == 'precip [mm/day]'
    assert mod._y_label('precip', da.pint.quantify()) == 'precip [mm/day]'
    da.attrs['long_name'] = 'IMERG daily precipitation'
    da.attrs['GRIB_name'] = 'Precipitation rate'
    assert mod._y_label('precip', da) == 'IMERG daily precipitation [mm/day]'

def test_datetime_axis_omits_default_time_label():
    import numpy as np
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    times = np.array(['2026-01-01', '2026-01-02'], dtype='datetime64[ns]')
    assert mod._is_datetime_axis(times)
    assert mod._resolve_time_axis_label(None, 'Valid time', times) == ''
    assert mod._resolve_time_axis_label('Lead time', 'time', times) == 'Lead time'
    doy = np.array([1, 2, 3])
    assert not mod._is_datetime_axis(doy)
    assert mod._resolve_time_axis_label(None, 'calendar day', doy) == 'Calendar day'

def test_date_ticks_are_calendar_dates_not_timestamps():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np
    from weather_skills_core.plot.figure import apply_date_ticks
    fig, ax = plt.subplots()
    days = mdates.date2num(np.arange('2026-08-05', '2026-09-10', dtype='datetime64[D]'))
    ax.plot(days, np.arange(len(days)))
    apply_date_ticks(ax)
    fig.canvas.draw()
    labels = [tick.get_text() for tick in ax.get_xticklabels() if tick.get_text()]
    assert labels
    assert all(('00:00' not in label for label in labels))
    assert all(("'" in label and any((ch.isalpha() for ch in label)) for label in labels))
    plt.close(fig)

def test_trace_label_prefers_station_id_over_tahmo_source():
    import numpy as np
    import xarray as xr
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    ds = xr.Dataset({'precip': (('time',), [1.0, 2.0])}, coords={'time': np.array(['2026-08-19', '2026-08-20'], dtype='datetime64[ns]'), 'station_id': 'TA00072', 'name': 'Likoni'})
    ds.attrs['weather_skills_source'] = 'tahmo'
    assert mod._trace_label(ds, 0) == 'TA00072 Likoni'

def test_trace_label_uses_filename_when_source_is_shared():
    import numpy as np
    import xarray as xr
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    ds = xr.Dataset({'precip': (('time',), [1.0, 2.0])}, coords={'time': np.array(['2026-08-19', '2026-08-20'], dtype='datetime64[ns]')})
    ds.attrs['weather_skills_source'] = 'tahmo'
    ds.encoding['source'] = '/tmp/ta00072.zarr'
    assert mod._trace_label(ds, 0) == 'ta00072'

def test_trace_label_respects_explicit_override():
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    ds = make_gridded(n_time=2)
    assert mod._trace_label(ds, 0, 'Custom name') == 'Custom name'

def test_day_of_year_tick_label():
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    assert mod._day_of_year_tick_label(1) == '1 Jan'
    assert mod._day_of_year_tick_label(274) == '1 Oct'
    assert mod._day_of_year_tick_label(366) == '31 Dec'

def test_apply_day_of_year_ticks(tmp_path, plot_timeseries):
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    assert mod._day_of_year_tick_label(1) == '1 Jan'
    assert 'Oct' in mod._day_of_year_tick_label(274)
    src = write_zarr(make_gridded(n_time=12, start='2023-01-01'), tmp_path / 'in.zarr')
    out = tmp_path / 'doy.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"align":"dayofyear","reduce":["latitude","longitude"]}]}')
    assert out.exists()

def test_bar_writes_png(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    out = tmp_path / 'bars.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_bar_grouped_multi_input_writes_png(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / 'a.zarr')
    b = write_zarr(make_gridded(fill=2.0), tmp_path / 'b.zarr')
    out = tmp_path / 'grouped.png'
    run_skill(plot_timeseries, '-i', str(a), '-i', str(b), '-o', str(out), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]}],"title":"Grouped"}')
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]['skill'] == 'plot-timeseries'
    assert history[-1]['args']['spec']['traces'][0]['mark'] == 'bar'

def test_bar_forecast_step_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast()
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'fc.zarr')
    out = tmp_path / 'bars.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_parse_trace_selector_and_aliases():
    import argparse
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    spec = mod.parse_trace('2026:color=black,lw=2.5,ms=7,zorder=5')
    assert spec.selector == '2026'
    assert spec.options == {'color': 'black', 'linewidth': 2.5, 'markersize': 7.0, 'zorder': 5.0}
    assert str(spec) == '2026:color=black,lw=2.5,ms=7,zorder=5'
    styled = mod.parse_trace('clim:mark=line,ls=--,lw=2.5')
    assert styled.options == {'mark': 'line', 'linestyle': '--', 'linewidth': 2.5}
    with pytest.raises(argparse.ArgumentTypeError, match='SELECTOR:k=v'):
        mod.parse_trace('black')
    with pytest.raises(argparse.ArgumentTypeError, match='unknown --trace option'):
        mod.parse_trace('1:colour=red')
    with pytest.raises(argparse.ArgumentTypeError, match='must be line or bar'):
        mod.parse_trace('1:mark=scatter')

def test_resolve_trace_styles_star_then_token():
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    labels = ['chirps_2006', 'chirps_2015', 'chirps_2026']
    styles = mod.resolve_trace_styles(labels, [mod.parse_trace('*:color=0.65,linewidth=1.2'), mod.parse_trace('2026:color=black,linewidth=2.5,zorder=5')])
    assert styles[0] == {'color': '0.65', 'linewidth': 1.2}
    assert styles[1] == {'color': '0.65', 'linewidth': 1.2}
    assert styles[2] == {'color': 'black', 'linewidth': 2.5, 'zorder': 5.0}

def test_resolve_trace_styles_index_and_unmatched():
    from weather_skills_core.errors import UsageError
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    labels = ['a', 'b']
    styles = mod.resolve_trace_styles(labels, [mod.parse_trace('2:color=red')])
    assert styles[0] == {}
    assert styles[1] == {'color': 'red'}
    with pytest.raises(UsageError, match='matched no series'):
        mod.resolve_trace_styles(labels, [mod.parse_trace('9:color=red')])
    with pytest.raises(UsageError, match='more than one series'):
        mod.resolve_trace_styles(['yr_2026_a', 'yr_2026_b'], [mod.parse_trace('2026:color=black')])

def test_along_dim_resolves_member_alias():
    import numpy as np
    import xarray as xr
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    da = xr.DataArray(np.ones((3, 4)), dims=('number', 'step'), coords={'number': [0, 1, 2], 'step': np.arange(4)})
    assert mod._along_dim(da, 'number') == 'number'
    assert mod._along_dim(da, 'member') == 'number'
    assert mod._along_dim(da, 'realization') == 'number'
    assert mod._along_dim(da, 'time') is None

def test_draw_lines_along_is_one_call_one_legend_entry():
    import numpy as np
    from weather_skills_core.plot.charts import compile_lines
    y = np.column_stack([np.arange(4.0), np.arange(4.0) + 1.0, np.arange(4.0) + 2.0])
    series = [([1, 2, 3, 4], y, 'ens')]
    fig = compile_lines(series, styles=[{}]).fig
    lines = fig.axes[0].lines
    assert len(lines) == 3
    assert lines[0].get_label() == 'ens'
    assert all((ln.get_label() == '_nolegend_' for ln in lines[1:]))

def test_draw_lines_along_cycle_uses_distinct_colors():
    import numpy as np
    from matplotlib.colors import to_hex
    from weather_skills_core.plot.charts import compile_lines
    y = np.column_stack([np.arange(4.0), np.arange(4.0) + 1.0, np.arange(4.0) + 2.0])
    series = [([1, 2, 3, 4], y, 'ens')]
    fig = compile_lines(series, styles=[{'along_color': 'cycle', 'along_labels': ['0', '1', '2']}]).fig
    lines = fig.axes[0].lines
    assert len({to_hex(ln.get_color()) for ln in lines}) == 3
    assert [ln.get_label() for ln in lines] == ['0', '1', '2']

def test_along_number_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast(members=5)
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'ens.zarr')
    out = tmp_path / 'spaghetti.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"],"along":"number"}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]['args']['spec']['traces'][0]['along'] == 'number'

def test_along_color_cycle_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast(members=3)
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'ens.zarr')
    out = tmp_path / 'cycle.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"],"along":"number","along_color":"cycle"}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]['args']['spec']['traces'][0]['along_color'] == 'cycle'

def test_band_with_along_writes_png(tmp_path, plot_timeseries):
    ds = make_forecast(members=8)
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'ens.zarr')
    out = tmp_path / 'band.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"],"along":"number","band":[10.0,90.0]}],"theme":{"template":"colorblind"}}')
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]['args']['spec']['traces'][0]['along'] == 'number'
    assert history[-1]['args']['spec']['traces'][0]['band'] == [10.0, 90.0]

def test_band_without_along_exits(tmp_path, plot_timeseries):
    ds = make_forecast(members=3)
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'ens.zarr')
    with pytest.raises(SystemExit):
        run_skill(plot_timeseries, '-i', str(src), '-o', str(tmp_path / 'no.png'), '--spec', '{"traces":[{"reduce":["latitude","longitude"],"band":[10.0,90.0]}]}')

def test_along_member_alias_and_1d_overlay(tmp_path, plot_timeseries):
    ens = make_forecast(members=4, fill=1.0)
    ens['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    obs = make_gridded(n_time=3, fill=2.0, name='tp')
    obs['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    ens_path = write_zarr(ens, tmp_path / 'ens.zarr')
    obs_path = write_zarr(obs, tmp_path / 'obs.zarr')
    out = tmp_path / 'overlay.png'
    run_skill(plot_timeseries, '-i', str(ens_path), '-i', str(obs_path), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"],"along":"member"},{"line":{"color":"black","linewidth":2.5,"alpha":1.0}}],"inputs":[{"label":"ens"},{"label":"obs"}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_along_101_members_is_one_input(tmp_path, plot_timeseries):
    ds = make_forecast(n_step=6, lats=(1.0,), lons=(10.0,), members=101, fill=0.5)
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'ens101.zarr')
    out = tmp_path / 'ens101.png'
    run_skill(plot_timeseries, '-i', str(src), '-o', str(out), '--spec', '{"traces":[{"reduce":["latitude","longitude"],"along":"number"}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0

def test_leftover_member_dim_suggests_along(tmp_path, plot_timeseries):
    ds = make_forecast(members=3)
    ds['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    src = write_zarr(ds, tmp_path / 'ens.zarr')
    with pytest.raises(SystemExit) as exc:
        run_skill(plot_timeseries, '-i', str(src), '-o', str(tmp_path / 'ts.png'), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}]}')
    assert exc.value.code == 2

def test_along_missing_dim_with_other_extras_exits(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    with pytest.raises(SystemExit) as exc:
        run_skill(plot_timeseries, '-i', str(src), '-o', str(tmp_path / 'ts.png'), '--spec', '{"traces":[{"along":"number"}]}')
    assert exc.value.code == 2

def test_along_bar_overlay_writes_png(tmp_path, plot_timeseries):
    ens = make_forecast(members=3, fill=1.0)
    ens['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    obs = make_gridded(n_time=3, fill=2.0, name='tp')
    obs['tp'].attrs.update(units='mm day-1', standard_name='lwe_precipitation_rate')
    obs_path = write_zarr(obs, tmp_path / 'obs.zarr')
    ens_path = write_zarr(ens, tmp_path / 'ens.zarr')
    out = tmp_path / 'bars_plus_ens.png'
    run_skill(plot_timeseries, '-i', str(obs_path), '-i', str(ens_path), '-o', str(out), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"],"along":"number"}],"inputs":[{"label":"obs"},{"label":"ens"}]}')
    assert Path(out).exists()

def test_draw_lines_applies_color_and_width():
    from matplotlib.colors import to_rgb
    from weather_skills_core.plot.charts import compile_lines
    from weather_skills_core.plot.theme import mpl_color
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    series = [([1, 2], [0.0, 1.0], 'chirps_2006'), ([1, 2], [1.0, 2.0], 'chirps_2026')]
    styles = mod.resolve_trace_styles(['chirps_2006', 'chirps_2026'], [mod.parse_trace('*:color=0.65'), mod.parse_trace('2026:color=black,linewidth=3')])
    fig = compile_lines(series, styles=styles).fig
    lines = fig.axes[0].lines
    assert to_rgb(lines[0].get_color()) == to_rgb(mpl_color('0.65'))
    assert to_rgb(lines[1].get_color()) == to_rgb('black')
    assert lines[1].get_linewidth() == 3

def test_trace_writes_png_and_stamps_args(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / 'chirps_2006.zarr')
    b = write_zarr(make_gridded(fill=2.0), tmp_path / 'chirps_2026.zarr')
    out = tmp_path / 'analogs.png'
    run_skill(
        plot_timeseries,
        '-i', str(a),
        '-i', str(b),
        '-o', str(out),
        '--spec',
        '{"traces":[{"reduce":["latitude","longitude"],"line":{"color":"0.65","linewidth":1.2}},{"line":{"color":"black","linewidth":2.5,"zorder":5}}]}',
    )
    assert Path(out).exists()
    history = load_figure_history(out)
    spec = history[-1]['args']['spec']
    assert spec['traces'][0]['line']['color'] == '0.65'
    assert spec['traces'][1]['line'] == {'color': 'black', 'linewidth': 2.5, 'zorder': 5}


def test_trace_bar_rejects_linewidth(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    with pytest.raises(SystemExit) as exc:
        run_skill(plot_timeseries, '-i', str(src), '-o', str(tmp_path / 'bars.png'), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"],"line":{"linewidth":3.0}}]}')
    assert exc.value.code == 2

def test_draw_mixed_bars_and_line():
    from weather_skills_core.plot.charts import compile_lines
    mod = load_skill('plot-timeseries', 'plot_timeseries')
    series = [([1.0, 2.0, 3.0], [1.0, 2.0, 1.5], 'obs'), ([1.0, 2.0, 3.0], [0.8, 1.1, 0.9], 'clim')]
    styles = mod.resolve_trace_styles(['obs', 'clim'], [mod.parse_trace('clim:mark=line,linestyle=--,linewidth=2.5,marker=none')])
    fig = compile_lines(series, kinds=['bar', 'line'], styles=styles).fig
    ax = fig.axes[0]
    handles, labels = ax.get_legend_handles_labels()
    assert 'obs' in labels
    assert 'clim' in labels
    assert len(ax.patches) == 3
    assert ax.lines[0].get_label() == 'clim'
    assert ax.lines[0].get_linewidth() == 2.5

def test_place_legend_below_axis():
    from weather_skills_core.plot.charts import compile_lines
    labels = ['2006 (analog)', '2015 (analog)', '2019 (analog)', '2023 (analog)', '2026 observed (CHIRPS)', '2026 ECMWF S2S members', 'ECMWF S2S ensemble mean']
    series = [([1, 2], [1.0 + 0.1 * i, 2.0 + 0.1 * i], label) for i, label in enumerate(labels)]
    fig = compile_lines(series, figsize=(16, 9)).fig
    fig.canvas.draw()
    ax = fig.axes[0]
    legend = ax.get_legend()
    assert legend is not None
    assert legend.get_window_extent().y1 < ax.get_window_extent().y0

def test_trace_per_series_style_bar_plus_line(tmp_path, plot_timeseries):
    obs = write_zarr(make_gridded(fill=1.0), tmp_path / 'obs.zarr')
    clim = write_zarr(make_gridded(fill=0.5), tmp_path / 'clim.zarr')
    out = tmp_path / 'obs_vs_clim.png'
    run_skill(plot_timeseries, '-i', str(obs), '-i', str(clim), '-o', str(out), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]},{"mark":"line","line":{"linestyle":"--","linewidth":2.5,"marker":"none"}}],"inputs":[{"label":"obs"},{"label":"clim"}]}')
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]['args']['spec']['traces'][0]['mark'] == 'bar'
    assert history[-1]['args']['spec']['traces'][1]['mark'] == 'line'
    assert history[-1]['args']['spec']['traces'][1]['line']['linestyle'] == '--'

def test_bar_mode_stacked_writes_png(tmp_path, plot_timeseries):
    a = write_zarr(make_gridded(fill=1.0), tmp_path / 'a.zarr')
    b = write_zarr(make_gridded(fill=0.5), tmp_path / 'b.zarr')
    out = tmp_path / 'stacked.png'
    run_skill(plot_timeseries, '-i', str(a), '-i', str(b), '-o', str(out), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]}],"layout":{"bar_mode":"stacked"}}')
    assert Path(out).exists()
    assert out.stat().st_size > 0
    history = load_figure_history(out)
    assert history[-1]['args']['spec']['layout']['bar_mode'] == 'stacked'

def test_trace_unmatched_selector_exits(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    with pytest.raises(SystemExit) as exc:
        run_skill(
            plot_timeseries,
            '-i', str(src),
            '-o', str(tmp_path / 'ts.png'),
            '--spec',
            '{"traces":[{"reduce":["latitude","longitude"]},{"input":"missing","line":{"color":"black"}}]}',
        )
    assert exc.value.code == 2


def test_replot_from_spec(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    first = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(src), '--dump-spec', str(tmp_path / 'ts.plot.json'), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]}],"title":"Original"}')
    spec_path = tmp_path / 'ts.plot.json'
    data = json.loads(spec_path.read_text())
    assert data['traces'][0]['mark'] == 'bar'
    assert data['traces'][0]['reduce'] == ['latitude', 'longitude']
    assert data['layout']['bar_mode'] == 'grouped'
    assert data['axes'] == {}
    assert not first.exists()
    data['title'] = 'Edited'
    data['axes'] = {'yticks': [0.0, 0.5, 1.0]}
    spec_path.write_text(json.dumps(data))
    second = tmp_path / 'ts2.png'
    second_spec = tmp_path / 'ts2.plot.json'
    run_skill(plot_timeseries, '--spec', str(spec_path), '--dump-spec', str(second_spec))
    assert not second.exists()
    replotted = json.loads(second_spec.read_text())
    assert replotted['axes']['yticks'] == [0.0, 0.5, 1.0]
    assert replotted['title'] == 'Edited'
    run_skill(plot_timeseries, '--spec', str(spec_path), '-o', str(second))
    assert second.is_file() and second.stat().st_size > 0
    history = load_figure_history(second)
    assert history[-1]['skill'] == 'plot-timeseries'
    assert history[-1]['input']['basename'] == 'in.zarr'

def test_dump_spec_bar_mode_stacked(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    out = tmp_path / 'ts.png'
    spec_path = tmp_path / 'ts.plot.json'
    run_skill(plot_timeseries, '-i', str(src), '--dump-spec', str(spec_path), '--spec', '{"traces":[{"mark":"bar","reduce":["latitude","longitude"]}],"layout":{"bar_mode":"stacked"}}')
    data = json.loads(spec_path.read_text())
    assert data['layout']['bar_mode'] == 'stacked'
    assert data['traces'][0]['mark'] == 'bar'
    assert not out.exists()

def test_patch_flag_merges_into_spec(tmp_path, plot_timeseries):
    src = write_zarr(make_gridded(), tmp_path / 'in.zarr')
    out = tmp_path / 'ts.png'
    run_skill(plot_timeseries, '-i', str(src), '--dump-spec', str(tmp_path / 'ts.plot.json'), '--spec', '{"traces":[{"reduce":["latitude","longitude"]}],"title":"Patched","axes":{"xticks":["2026-08-17"]}}')
    spec = json.loads((tmp_path / 'ts.plot.json').read_text())
    assert spec['title'] == 'Patched'
    assert spec['axes']['xticks'] == ['2026-08-17']
    assert 'patch' not in spec
    assert not Path(out).exists()


def test_per_input_variable(tmp_path, plot_timeseries):
    precip = make_gridded(name='precip', fill=1.0)
    temp = make_gridded(name='t2m', fill=2.0)
    temp['t2m'].attrs.update(units='K', standard_name='air_temperature')
    a = write_zarr(precip, tmp_path / 'precip.zarr')
    b = write_zarr(temp, tmp_path / 'temp.zarr')
    out = tmp_path / 'ts.png'
    run_skill(
        plot_timeseries,
        '-i', str(a), '-i', str(b), '-o', str(out),
        '--spec',
        '{"inputs":[{"id":"a","variable":"precip"},{"id":"b","variable":"t2m"}],'
        '"traces":[{"reduce":["latitude","longitude"]}],"layout":{"facet":{"per_trace":true}}}',
    )
    assert out.exists()


def test_per_trace_reduce_overrides_trace0(tmp_path, plot_timeseries, capsys):
    a = write_zarr(make_gridded(), tmp_path / 'a.zarr')
    b = write_zarr(make_gridded(fill=2.0), tmp_path / 'b.zarr')
    with pytest.raises(SystemExit):
        run_skill(
            plot_timeseries,
            '-i', str(a), '-i', str(b), '-o', str(tmp_path / 'ts.png'),
            '--spec',
            '{"traces":[{"reduce":["latitude","longitude"]},{"input":"b","reduce":["latitude"]}]}',
        )
    err = capsys.readouterr().err
    assert 'input 2' in err
    assert 'traces[].reduce' in err
    assert 'longitude' in err
