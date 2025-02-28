import sys
import os.path
import socket
import datetime
import math

from openmm import app
import openmm as mm
import openmm.unit as u

from miniomm.config import Config
import miniomm.util as util
from miniomm.namdbin import NAMDBin
from miniomm.namdxsc import write_xsc
import miniomm.atomrestraint as atrest


# Order of priority for the operation to perform
#   Minimization if minim.coor absent
#   NPT if equilib.coor absent
#   NVT otherwise

# Order of priority for box size
#   Checkpoint file
#   Box size (input.xsc)

# Order of priority for starting coordinates
#   Checkpoint file
#   Input coordinates (input.coor)
#   PDB coordinates (structure.pdb)


# Order of priority for starting velocities
#   Checkpoint file
#   Input velocities (input.vel)
#   Randomized velocities

checkpoint_file = "miniomm_restart.chk"
DEF_CUTOFF = 9.0
DEF_SWITCHDIST = 7.5
DEF_FRICTION = 0.1


def _printPluginInfo():
    lp = mm.version.openmm_library_path
    print(
        f"""
           $OPENMM_CUDA_COMPILER: {os.environ.get('OPENMM_CUDA_COMPILER','(Undefined)')}
      OpenMM Library Path ($OMM): {lp}
                  Loaded Plugins: """,
    )
    for p in mm.pluginLoadedLibNames:
        print("                                  " + p.replace(lp, "$OMM"))
    print("            Loaded Plugin errors:")
    for e in mm.Platform.getPluginLoadFailures():
        print("                                  " + e)
    print("\n")


def run_omm(options):

    inp = Config(options.input)

    dt = float(inp.getWithDefault("timestep", 4)) * u.femtosecond
    temperature = float(inp.getWithDefault("temperature", 300)) * u.kelvin
    thermostattemperature = (
        float(inp.getWithDefault("thermostattemperature", 300)) * u.kelvin
    )
    logPeriod = 1 * u.picosecond
    trajectoryPeriod = int(inp.getWithDefault("trajectoryperiod", 25000)) * dt
    run_steps = int(inp.run)
    basename = "output"
    trajectory_file = basename + ".dcd"

    if "PME" in inp and not inp.getboolean("PME"):
        nonbondedMethod = app.NoCutoff
    else:
        nonbondedMethod = app.PME
    nonbondedCutoff = float(inp.getWithDefault("cutoff", DEF_CUTOFF)) * u.angstrom
    switchDistance = (
        float(inp.getWithDefault("switchdistance", DEF_SWITCHDIST)) * u.angstrom
    )
    frictionCoefficient = (
        float(inp.getWithDefault("thermostatdamping", DEF_FRICTION)) / u.picosecond
    )

    endTime = run_steps * dt

    util.check_openmm()

    if options.platform is None:
        print("Selecting best platform:")
        req_platform_name = util.get_best_platform()
    else:
        print(f"Requesting platform {options.platform}")
        req_platform_name = options.platform
    req_platform = mm.Platform.getPlatformByName(req_platform_name)

    req_properties = {}  # {'UseBlockingSync':'true'}
    if options.device is not None and "DeviceIndex" in req_platform.getPropertyNames():
        print(f"    Setting DeviceIndex = {options.device}")
        req_properties["DeviceIndex"] = str(options.device)
    if options.precision is not None and "Precision" in req_platform.getPropertyNames():
        print("    Setting Precision = " + options.precision)
        req_properties["Precision"] = options.precision

    # Same logic as https://software.acellera.com/docs/latest/acemd3/reference.html
    if dt > 2 * u.femtosecond:
        hydrogenMass = 4 * u.amu
        constraints = app.AllBonds
        rigidWater = True
    elif dt > 0.5 * u.femtosecond:
        hydrogenMass = None
        constraints = app.HBonds
        rigidWater = True
    else:
        hydrogenMass = None
        constraints = None
        rigidWater = False

    print(
        f"""
                            Host: {socket.gethostname()}
                            Date: {datetime.datetime.now().ctime()}
                        Timestep: {dt}
                     Constraints: {constraints}
                     Rigid water: {rigidWater}
                       Nonbonded: {nonbondedMethod}
    Hydrogen mass repartitioning: {hydrogenMass}
    """
    )
    _printPluginInfo()


    # -------------------------------------------------------
    run_type = None
    if "parmfile" in inp:
        run_type = "AMBER"
    elif "parameters" in inp:
        run_type = "CHARMM"
    elif "openmmsystem" in inp:
        run_type = "OpenMM"
    else:
        raise ValueError("Could not detect run type (AMBER/CHARMM/OpenMM)")

    
    # -------------------------------------------------------
    if run_type == "AMBER":
        print(f"Creating an AMBER system...")
        if "structure" in inp:
            print("Warning: 'structure' given but ignored for AMBER")
        prmtop = app.AmberPrmtopFile(inp.parmfile)
        system = prmtop.createSystem(
            nonbondedMethod=nonbondedMethod,
            nonbondedCutoff=nonbondedCutoff,
            switchDistance=switchDistance,
            constraints=constraints,
            hydrogenMass=hydrogenMass,
            rigidWater=rigidWater,
        )
        topology = prmtop.topology
    elif run_type == "CHARMM":
        print(f"Creating a CHARMM system...")
        psf = app.CharmmPsfFile(inp.structure)
        try:
            params = app.CharmmParameterSet(inp.parameters, permissive=False)
        except Exception as e:
            print("** Error reported: " + str(e))
            print(
                "** Error reading parameter set. Make sure the ATOMS section and MASS items are present in the parameter file."
            )
            raise e
        # The following is necessary otherwise PME fails. Box is replaced later
        psf.setBox(50.0 * u.angstrom, 50.0 * u.angstrom, 50.0 * u.angstrom)  
        system = psf.createSystem(
            params,
            nonbondedMethod=nonbondedMethod,
            nonbondedCutoff=nonbondedCutoff,
            switchDistance=switchDistance,
            constraints=constraints,
            hydrogenMass=hydrogenMass,
            rigidWater=rigidWater,
        )
        topology = psf.topology
    elif run_type == "OpenMM":
        print(f"Creating an OpenMM XML system...")
        system = mm.XmlSerializer.deserialize(open(inp.openmmsystem).read())
        topology = app.PDBFile(inp.structure).topology
        

        

    # -------------------------------------------------------
    if "barostat" in inp and inp.getboolean("barostat"):
        pressure = float(inp.barostatpressure) * u.bar
        print(f"Enabling barostat at {pressure}...")
        system.addForce(mm.MonteCarloBarostat(pressure, thermostattemperature))

    if "plumedfile" in inp:
        print("Attempting to load PLUMED plugin...")
        from openmmplumed import PlumedForce

        plines = util.plumed_parser(inp.plumedfile)
        system.addForce(PlumedForce(plines))

    integrator = mm.LangevinIntegrator(thermostattemperature, frictionCoefficient, dt)
    integrator.setConstraintTolerance(1e-5)

    simulation = app.Simulation(
        topology, system, integrator, req_platform, req_properties
    )
    ctx = simulation.context
    platform = ctx.getPlatform()
    print(f"Got platform {platform.getName()} with properties:")
    for prop in platform.getPropertyNames():
        print(f"    {prop}\t\t{platform.getPropertyValue(ctx,prop)}")
    print("")

    resuming = False
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file, "rb") as cf:
            ctx.loadCheckpoint(cf.read())
        # ctx.loadCheckpoint(str(checkpoint_file))
        util.round_state_time(ctx, 10 * dt)
        print(f"Successfully loaded {checkpoint_file}, resuming simulation...")
        resuming = True

    elif "openmmstate" in inp:
        print(f"Attempting resume from OpenMM state {inp.openmmstate}...")
        simulation.loadState(inp.openmmstate)
        
    else:
        print(
            f"File {checkpoint_file} absent, starting simulation from the beginning..."
        )
        coords = util.get_coords(inp)
        ctx.setPositions(coords)
        if not resuming:
            (boxa, boxb, boxc) = util.get_box_size(inp)
            ctx.setPeriodicBoxVectors(boxa, boxb, boxc)

        if "minimize" in inp:
            print(f"Minimizing for max {inp.minimize} iterations...")
            simulation.minimizeEnergy(maxIterations=int(inp.minimize))
            simulation.saveState(f"miniomm_minimized.xml")
        else:
            if "binvelocities" in inp:
                print(f"Reading velocities from NAMDBin: " + inp.binvelocities)
                vels = NAMDBin(inp.binvelocities).getVelocities()
                ctx.setVelocities(vels)
            else:
                print(f"Resetting thermal velocities at {temperature}")
                ctx.setVelocitiesToTemperature(temperature)


        # -------------------------------------------------------
    if "atomrestraint" in inp:
        print("\nApplying restraints...")
        atrest_dict = atrest.atrest_parser(inp.get("atomrestraint"), run_steps, dt)
        atrest.add_restraints(simulation, atrest_dict)
        print(
            f'Restraints summary:\n'
            f'  With the selection "{atrest_dict["selection"]}", {atrest_dict["n_atoms"]} atoms have been restrained.\n'
            f'  Forces acting on {atrest_dict["axes"]} axes.\n'
            f'  Number of setpoints: {len(atrest_dict["setpoints"])}'
            )
        for setpoint in atrest_dict["setpoints"]:
            time_in_ps = (setpoint["step"] * dt).value_in_unit(u.picoseconds)
            print(f'    - {setpoint["force"]} kcal/(mol*A^2) at step {setpoint["step"]} ({time_in_ps} ps)')


    # -------------------------------------------------------
    print("")
    inp.printWarnings()

    # -------------------------------------------------------
    print("")

    startTime = ctx.getState().getTime()
    startTime_f = startTime.in_units_of(u.nanoseconds).format("%.3f")
    endTime_f = endTime.in_units_of(u.nanoseconds).format("%.3f")
    remaining_steps = round((endTime - startTime) / dt)
    remaining_ns = (remaining_steps * dt).value_in_unit(u.nanosecond)

    log_every = util.every(logPeriod, dt)
    save_every = util.every(trajectoryPeriod, dt)
    if remaining_steps % save_every != 0:
        raise ValueError("Remaining steps is not a multiple of trajectoryperiod")

    print(
        f"Current simulation time is {startTime_f}, running up to {endTime_f}:\n"
        f"  will run for {remaining_steps} timesteps = {remaining_ns:.3f} ns,\n"
        f"  logging every {logPeriod} ({log_every} steps),\n"
        f"  saving frames every {trajectoryPeriod.in_units_of(u.picosecond)} ({save_every} steps),\n"
        f"  checkpointing on {checkpoint_file}.\n"
    )

    util.add_reporters(
        simulation,
        trajectory_file,
        log_every,
        save_every,
        remaining_steps,
        resuming,
        checkpoint_file,
    )

    # ----------------------------------------
    simulation.saveState(f"miniomm_pre.xml")

    # ----------------------------------------
    if "atomrestraint" not in inp:
        simulation.step(remaining_steps)
    else:
        starting_step = run_steps - remaining_steps
        starting_percent = math.floor(starting_step/run_steps * 1000)
        force, gradient = atrest.get_starting_force_and_gradient(starting_percent, atrest_dict)
        simulation.context.setParameter('k', force)

        for current_percent in range(starting_percent, 1000):

            for atrest_setpoint in atrest_dict["setpoints"]:
                if current_percent == atrest_setpoint["percent"]:
                    force = atrest_setpoint["force"]
                    gradient = atrest.get_force_gradient(atrest_setpoint, atrest_dict)
                    simulation.context.setParameter('k', force)

            simulation.step(int(run_steps/1000))

            force += gradient
            simulation.context.setParameter('k', force)

    # ----------------------------------------
    simulation.saveState(f"miniomm_post.xml")
    final_state = simulation.context.getState(getPositions=True, getVelocities=True)
    final_coor = final_state.getPositions(asNumpy=True)
    NAMDBin(final_coor).write_file(f"{basename}.coor")

    final_box = final_state.getPeriodicBoxVectors(asNumpy=True)
    write_xsc(f"{basename}.xsc", remaining_steps, final_state.getTime(), final_box)
    # ----------------------------------------
    print("Done!")
    return
