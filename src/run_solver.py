from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from functools import partial
import math

import argparse
#import os

import read_csv as reader
import vehicles as V
import demand as D
import evaluators as E
import solution_output as SO

def main():
    """Entry point of the program."""
    parser = argparse.ArgumentParser(description='Solve assignment of truck load routing problem, give hours of service rules and a specified list of origins and destinations')
    parser.add_argument('-m,--matrixfile', type=str, dest='matrixfile',
                        help='CSV file for travel matrix (distances)')
    parser.add_argument('-d,--demandfile', type=str, dest='demand',
                        help='CSV file for demand pairs (origin, dest, time windows)')
    parser.add_argument('--speed', type=float, dest='speed', default=55.0,
                        help='Average speed, miles per hour.  Default is 55 (miles per hour).  Distance unit should match that of the matrix of distances.  The time part should be per hours')
    parser.add_argument('--maxtime', type=float, dest='horizon', default=10080,
                        help='Max time in minutes.  Default is 10080 minutes, which is 7 days.')

    parser.add_argument('-v,--vehicles', type=int, dest='numvehicles', default=100,
                        help='Number of vehicles to create.  Default is 100.')
    parser.add_argument('--pickup_time', type=int, dest='pickup_time', default=15,
                        help='Pick up time in minutes.  Default is 15 minutes.')
    parser.add_argument('--dropoff_time', type=int, dest='dropoff_time', default=15,
                        help='Drop off time in minutes.  Default is 15 minutes.')

    parser.add_argument('-t, --timelimit', type=int, dest='timelimit', default=5,
                        help='Maximum run time for solver, in minutes.  Default is 5 minutes.')


    args = parser.parse_args()

    print('read in demand data')
    d = D.Demand(args.demand,args.horizon)

    print('read in distance matrix')
    matrix = reader.load_matrix_from_csv(args.matrixfile)

    # convert nodes to solver space from input map space
    matrix = d.generate_solver_space_matrix(matrix)
    minutes_matrix = reader.travel_time(args.speed/60,matrix)

    # maybe the dummy nodes are NOT needed?
    expanded_mm = minutes_matrix

    # copy to distance matrix
    expanded_m = reader.travel_time(60/args.speed,expanded_mm)
    # print('original matrix of',len(matrix.index),'expanded to ',len(expanded_m.index))

    # vehicles:
    vehicles = V.Vehicles(args.numvehicles)



    # Create the routing index manager.

    # number of nodes is now given by the travel time matrix
    # probably should refactor to put time under control of
    # demand class
    num_nodes = len(expanded_mm.index)
    print('solving with ',num_nodes,'nodes')

    # assuming here that all depots are in the same place
    # and that vehicles all return to the same depot
    manager = pywrapcp.RoutingIndexManager(
        num_nodes,
        len(vehicles.vehicles),
        vehicles.vehicles[0].depot_index)


    # Set model parameters
    # model_parameters = pywrapcp.DefaultRoutingModelParameters()
    # Create Routing Model.
    # routing = pywrapcp.RoutingModel(manager,model_parameters)
    routing = pywrapcp.RoutingModel(manager)
    #solver = routing.solver()
    print('creating time and distance callbacks for solver')
    # Define cost of each arc using travel time + service time
    time_callback = partial(E.create_time_callback(expanded_mm,
                                                   d),
                            manager)

    dist_callback = partial(E.create_dist_callback(expanded_m,
                                                   d),
                            manager)

    print('registering callbacks with routing solver')

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
    # might want to remove service time from the above

    print('create count dimension')
    # Add Count dimension for count windows, precedence constraints
    count_dimension_name = 'Count'
    routing.AddConstantDimension(
        1, # increment by one every time
        len(expanded_mm.index),  # max count is visit all the nodes
        True,  # set count to zero
        count_dimension_name)
    count_dimension = routing.GetDimensionOrDie(count_dimension_name)


    print('create time dimension')
    # Add Time dimension for time windows, precedence constraints
    time_dimension_name = 'Time'
    routing.AddDimension(
        transit_callback_index, # same "cost" evaluator as above
        args.horizon,  # slack for full range
        args.horizon,  # max time is end of time horizon
        True,  # do or  don't set time to zero...vehicles can wait at depot if necessary
        time_dimension_name)
    time_dimension = routing.GetDimensionOrDie(time_dimension_name)
    # this is new in v7.0, not sure what it does yet
    # time_dimension.SetGlobalSpanCostCoefficient(100)
    # Define Transportation Requests.

    demand_evaluator_index = routing.RegisterUnaryTransitCallback(
        partial(E.create_demand_callback(expanded_m.index,d), manager))

    print('create capacity dimension')
    # Add capacity dimension.  One load per vehicle
    cap_dimension_name = 'Capacity'
    vehicle_capacities = [veh.capacity for veh in vehicles.vehicles]
    routing.AddDimensionWithVehicleCapacity(
        demand_evaluator_index,
        0,  # null capacity slack
        vehicle_capacities,
        True,  # start cumul to zero
        cap_dimension_name)


    # [START pickup_delivery_constraint]
    print('apply pickup and delivery constraints')
    for idx in d.demand.index:
        record = d.demand.loc[idx]
        pickup_index = manager.NodeToIndex(record.origin)
        delivery_index = manager.NodeToIndex(record.destination)
        routing.AddPickupAndDelivery(pickup_index, delivery_index)
        routing.solver().Add(
            routing.VehicleVar(pickup_index) ==
            routing.VehicleVar(delivery_index))
        routing.solver().Add(
            time_dimension.CumulVar(pickup_index) <=
            time_dimension.CumulVar(delivery_index))


    # [START time_window_constraint]
    print('apply time window  constraints')
    for idx in d.demand.index:
        record = d.demand.loc[idx]
        pickup_index = manager.NodeToIndex(record.origin)
        early = int(record.early)
        late = int(record.late)
        time_dimension.CumulVar(pickup_index).SetRange(early, late)
        routing.AddToAssignment(time_dimension.SlackVar(pickup_index))
    # Add time window constraints for each vehicle start node
    # and 'copy' the slack var in the solution object (aka Assignment) to print it
    for vehicle in vehicles.vehicles:
        vehicle_id = vehicle.index
        index = routing.Start(vehicle_id)
        time_dimension.CumulVar(index).SetRange(vehicle.time_window[0],
                                                vehicle.time_window[1])
        routing.AddToAssignment(time_dimension.SlackVar(index))



    # [START breaks logic]
    print('apply break rules')
    # loosely copying code from google example, then modify
    node_visit_transit = {}
    for n in expanded_mm.index:
        node_visit_transit[n] = int(d.get_service_time(n))

    breaks = {}
    starts = []
    ends   = []
    # grab ref to solver
    solver = routing.solver()
    pickups = d.origins.index # dive deep into the internals of demand...

    for i in range(0,len(vehicles.vehicles)):
    # for i in [0,1]:
        print ( 'breaks for vehicle',i)
        breaks[i] = []
        # 10 hour breaks
        # first break is based on start of route.
        # for now, implementing the 11-hour limit as if the
        # time_dimension is only collecting driving time
        route_start = time_dimension.CumulVar(routing.Start(i))# + time_dimension.SlackVar(routing.Start(i))
        starts.append(route_start)
        route_end = time_dimension.CumulVar(routing.End(i))
        ends.append(route_end)
        must_start = route_start + 11*60 # 11 hours later
        print(route_start,must_start)

        if i == 0:
            # non optional first break for first vehicle
            first_10hr_break = solver.FixedDurationIntervalVar(
                # route_start, # minimum start time
                must_start,  # maximum start time (11 hours after start)
                10 * 60,     # duration of break is 10 hours
                'first 10hr break for vehicle {}'.format(i))
            breaks[i].append(first_10hr_break)
        else:
            # optional first break for other vehicles
            # hack to create a variable to determine if vehicle is used.
            vehicle_check = 1

            for node in pickups:
                index = manager.NodeToIndex(int(node))
                vehicle_at_node = routing.VehicleVar(manager.NodeToIndex(node))
                vehicle_condition = vehicle_at_node == i
                vehicle_check *= vehicle_condition
            print(vehicle_check)

            first_10hr_break = solver.FixedDurationIntervalVar(
                route_start, # minimum start time
                # must_start,  # maximum start time (11 hours after start)
                10 * 60,     # duration of break is 10 hours
                vehicle_check, # performed variable?? not sure
                'first 10hr break for vehicle {}'.format(i))
            breaks[i].append(first_10hr_break)
            trip_start = solver.FixedDurationIntervalVar(
                route_start,
                11,
                'initial drive vehicle {}'.format(i))

            # first_10hr_break = solver.FixedDurationIntervalVar(
            #         0, # minimum start time
            #         11,  # maximum start time (11 hours after start)
            #         600,     # duration of break is 10 hours
            #         True,       # optional == True, required == False
            #         'first 10hr break for vehicle {}'.format(i))
            # breaks[i].append(first_10hr_break)

            # timing of the break
            follow_after_constraint = first_10hr_break.StartsAfterEnd(
                trip_start)
            solver.AddConstraint(follow_after_constraint)
        # make the break optional if the vehicle is not used

        # quasi-conditional constraint.  Only perform break if vehicle is used

        # first, requirement that break is not performed
        # nobreak_condition = first_10hr_break.PerformedExpr()==False

        # second, the condition.  If route does not start, no break

        # vehicle_not_used = routing.IsEnd(routing.Start(i))
        # # print('vehicle is used expression',vehicle_not_used)

        # route_starts = count_dimension.CumulVar(routing.End(i)) >=2
        # print(route_starts)
        # solver.AddConstraint(
        #     first_10hr_break.PerformedExpr() == routing.IsEnd(routing.NextVar(routing.Start(i)))
        # )
        # and a conditional based on time
        # # # first, requirement that break is performed
        # break_condition = first_10hr_break.PerformedExpr()==1

        # second, the timing.  If route starts, need to break
        # trying out counting dimension
        # time_condition =  time_dimension.CumulVar(routing.Start(i)) >= 0

        # # use conditional expressions
        # expression = solver.ConditionalExpression(vehicle_check,
        #                                           break_condition,
        #                                           1)
        # solver.AddConstraint(
        #         expression >= 1
        #     )



        # now add additional breaks for whole of likely range
        # break up full time (horizon) into 10+11 hour ranges (drive 11, break 10)
        # not quite right, as the 14hr rule also comes into play
        # need_breaks = math.floor(args.horizon / 60 / (10 + 11))
        need_breaks = 9
        # follow_constraints = []
        # don't need first break, as that is already specified above
        for intvl in range(1,need_breaks):
            # break minimum start time is 0
            # break maximum start time is horizon - 10 hours

            min_start_time = 0 + (intvl - 1)*(10+11)*60
            max_start_time = args.horizon - 10*60

            optional_break = False
            if intvl > 0:
                optional_break = True
            # key on first break, but only required if time hasn't run out
            next_10hr_break = solver.FixedDurationIntervalVar(
                min_start_time, # minimum start time
                max_start_time,  # maximum start time (11 hours after start)
                10 * 60,     # duration of break is 10 hours
                optional_break,       # optional == True, required == False
                '{}th 10hr break for vehicle {}'.format(intvl,i))

            breaks[i].append(next_10hr_break)
            # constraints:
            # sync with preceding break
            #  this break starts 11h after end of prior
            follow_after_constraint = next_10hr_break.StartsAfterEndWithDelay(
                breaks[i][intvl-1],
                660) # 11 hours times 60 minutes = 660
            solver.AddConstraint(follow_after_constraint)


            if optional_break:
                # conditional constraint.  If vehicle is done before start
                # time, then don't bother with this break

                # first, requirement that break is performed
                break_condition = next_10hr_break.PerformedExpr()==True

                # second, the timing.  If route is over, don't need break
                break_start = route_start + intvl*(11+10)*60
                time_condition =  break_start < route_end # break_start

                # use conditional expression
                expression = solver.ConditionalExpression(time_condition,
                                                          break_condition,
                                                          1)
                solver.AddConstraint(
                    expression >= 1
                )

            # print(follow_after_constraint)
            # follow_constraints.append(follow_after_constraint)

        time_dimension.SetBreakIntervalsOfVehicle(
            breaks[i], i, node_visit_transit)

        # for follow_after_constraint  in follow_constraints:
        #     solver.Add(follow_after_constraint)

    # did it work?
    print('breaks done')

    # Setting first solution heuristic.
    # [START parameters]
    print('set up model parameters')
    # [START parameters]
    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION)

    # Disabling path Large Neighborhood Search is the default behaviour.  enable
    parameters.local_search_operators.use_path_lns = pywrapcp.BOOL_TRUE
    parameters.local_search_operators.use_inactive_lns = pywrapcp.BOOL_TRUE
    # Routing: forbids use of TSPOpt neighborhood,
    # parameters.local_search_operators.use_tsp_opt = pywrapcp.BOOL_FALSE
    # set a time limit
    parameters.time_limit.seconds = args.timelimit * 60   # timelimit minutes
    # sometimes helps with difficult solutions
    parameters.lns_time_limit.seconds = 1000  # 1000 milliseconds
    # i think this is the default
    # parameters.use_light_propagation = False
    # set to true to see the dump of search iterations
    parameters.log_search = pywrapcp.BOOL_TRUE

    # add disjunctions to deliveries to make it not fail
    penalty = 10000000  # The cost for dropping a demand node from the plan.
    break_penalty = 0  # The cost for dropping a break node from the plan.
    # all nodes are droppable, so add disjunctions

    droppable_nodes = []
    for c in expanded_mm.index:
        if c == 0:
            # no disjunction on depot node
            continue
        p = penalty
        if d.get_demand(c) == 0:
            # no demand means break node
            p = break_penalty
        droppable_nodes.append(routing.AddDisjunction([manager.NodeToIndex(c)],
                                                      p))


    print('Calling the solver')
    # [START solve]
    assignment = routing.SolveWithParameters(parameters)
    # [END solve]

    if assignment:
        ## save the assignment, (Google Protobuf format)
        #save_file_base = os.path.realpath(__file__).split('.')[0]
        #if routing.WriteAssignment(save_file_base + '_assignment.ass'):
        #    print('succesfully wrote assignment to file ' + save_file_base +
        #          '_assignment.ass')

        print('The Objective Value is {0}'.format(assignment.ObjectiveValue()))
        print('details:')
        SO.print_solution(d,dist_callback,vehicles,manager,routing,assignment)


    else:
        print('assignment failed')



if __name__ == '__main__':
    main()
