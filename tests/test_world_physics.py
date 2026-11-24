import networkx as nx
from shapely.geometry import Point

from case.physics import Environment, Event, FaultInjector, Fault, ramp
from case.world import load, world_summary


def test_world_is_deterministic_and_connected():
    w = load()
    s = world_summary(w)
    assert s["buildings"] > 3000 and s["sensor_total"] == 95
    assert s["sensors"] == {"water_level": 16, "air_quality": 27, "traffic": 40, "bridge_strain": 5, "weather": 4, "power_load": 3}
    assert nx.is_strongly_connected(w.roads)
    assert len({x.id for x in w.sensors}) == len(w.sensors)


def test_sensors_sit_where_they_should():
    w = load()
    for s in w.sensors:
        p = Point(s.xy)
        if s.type == "water_level" and not s.meta.get("creek"):
            assert w.water.boundary.distance(p) < 5          # on the shoreline
        if s.type == "air_quality":
            assert not w.water.contains(p)                    # on land


def test_ramp_shape():
    assert ramp(50, 100, 1000) == 0 and ramp(1100, 100, 1000) == 0
    assert ramp(550, 100, 1000, rise=900, fall=900) == 0.5
    assert ramp(500, 100, 1000, rise=100, fall=100) == 1.0


def test_plume_downwind_only_and_flood_decays():
    w = load()
    env = Environment(w)
    env.wind_toward_deg = 90.0                                # blowing east
    env.events.append(Event("p", "plume", 0, 10000, (0.0, 0.0), {"rise": 0, "fall": 0}))
    assert env.plume((500.0, 0.0), 100) > 10                  # downwind
    assert env.plume((-500.0, 0.0), 100) == 0                 # upwind
    assert env.plume((500.0, 0.0), 100) > env.plume((500.0, 300.0), 100)
    env.events.append(Event("f", "flood", 0, 10000, (0.0, 0.0), {"rise": 0, "fall": 0, "radius": 600, "peak": 0.7}))
    assert env.flood_rise((0, 0), 100) > env.flood_rise((600, 0), 100) > env.flood_rise((1500, 0), 100)


def test_fault_injector_kinds():
    fi = FaultInjector(seed=1)
    fi.faults = [Fault("a", "s1", "stuck", 10, 20), Fault("b", "s2", "dropout", 10, 20),
                 Fault("c", "s3", "offset", 10, 20, {"delta": 5})]
    fi.apply("s1", 9, 1.0)
    assert fi.apply("s1", 12, 3.0) == fi.apply("s1", 15, 7.0)      # frozen
    assert fi.apply("s2", 12, 1.0) is None                           # silent
    assert fi.apply("s3", 12, 1.0) == 6.0
    assert fi.apply("s3", 25, 1.0) == 1.0                            # over
