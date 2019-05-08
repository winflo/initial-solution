from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
from functools import partial
import math
import numpy as np

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
    parser.add_argument('--maxtime', type=int, dest='horizon', default=10080,
                        help='Max time in minutes.  Default is 10080 minutes, which is 7 days.')

    parser.add_argument('-v,--vehicles', type=int, dest='numvehicles', default=100,
                        help='Number of vehicles to create.  Default is 100.')
    parser.add_argument('--pickup_time', type=int, dest='pickup_time', default=15,
                        help='Pick up time in minutes.  Default is 15 minutes.')
    parser.add_argument('--dropoff_time', type=int, dest='dropoff_time', default=15,
                        help='Drop off time in minutes.  Default is 15 minutes.')

    parser.add_argument('-t, --timelimit', type=int, dest='timelimit', default=5,
                        help='Maximum run time for solver, in minutes.  Default is 5 minutes.')

    parser.add_argument('--expand', type=bool, dest='expand', default=False,
                        help="Expand the time matrix to break long links.  Can help solver find a solution.  Defaults to False, which means to just use the input matrix.")
    parser.add_argument('--maxlinktime', type=int, dest='timelength', default=600,
                        help='If expand is true, this sets the maximum time for segments in the network.  Default is 600 minutes, or 10 hours.')


    args = parser.parse_args()

    print('read in distance matrix')
    matrix = reader.load_matrix_from_csv(args.matrixfile)
    minutes_matrix = reader.travel_time(args.speed/60,matrix)

    print('read in demand data')
    d = D.Demand(args.demand,minutes_matrix,args.horizon)

    # convert nodes to solver space from input map space
    mm = d.generate_solver_space_matrix(minutes_matrix,args.horizon)
    # ditto for space
    # m = reader.travel_time(60/args.speed,minutes_matrix)

    # create dummy nodes every 20 hours
    # expanded_mm = minutes_matrix
    # might want to expand matrix, but I don't see any benefit from this
    if args.expand:
        expanded_mm = d.make_break_nodes(mm,args.timelength)
    else:
        expanded_mm = mm
    # print(expanded_mm)

    # copy to distance matrix
    expanded_m = reader.travel_time(60/args.speed,expanded_mm)
    # print('original matrix of',len(matrix.index),'expanded to ',len(expanded_m.index))

    # vehicles:
    vehicles = V.Vehicles(args.numvehicles,args.horizon)



    # Create the routing index manager.

    # number of nodes is now given by the travel time matrix
    # probably should refactor to put time under control of
    # demand class
    num_nodes = len(expanded_mm.index)
    print('solving with ',num_nodes,'nodes')
    print(d.demand.loc[d.demand.feasible,:])
    # print(expanded_mm)
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
    print('creating time callback for solver')
    # Define cost of each arc using travel time + service time
    time_callback = partial(E.create_time_callback(expanded_mm,
                                                   d),
                            manager)

    # print('creating distance callbacks for solver')
    # dist_callback = partial(E.create_dist_callback(expanded_m,
    #                                                d),
    #                         manager)

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
        # True, # set to zero for each vehicle
        False,  # don't set time to zero...vehicles can wait at depot if necessary
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
        if not record.feasible:
            continue
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
        if not record.feasible:
            continue
        pickup_index = manager.NodeToIndex(record.origin)
        early = int(record.early)# 0
        late = int(record.late)  #  + args.horizon
        time_dimension.CumulVar(pickup_index).SetRange(early, late)
        routing.AddToAssignment(time_dimension.SlackVar(pickup_index))
        # and  add simulation-wide time windows (slack) for delivery nodes,
        dropoff_index = manager.NodeToIndex(record.destination)
        tt = expanded_mm.loc[record.origin,record.destination]
        # early time windows is minimal breaks: start fresh, drive straight
        breaks = math.floor(tt/60/11) * 600
        early = int(record.early + tt + breaks)
        # late time window  maybe another break will have to be inserted
        breaks += 600
        late = int(record.late + tt + breaks)
        time_dimension.CumulVar(dropoff_index).SetRange(early, late)
        routing.AddToAssignment(time_dimension.SlackVar(dropoff_index))


    for node in range(mm.index.max()+1,expanded_mm.index.max()+1):
        # default is to give dummy nodes infinite time windows
        start = 0
        end = args.horizon
        # don't do that for dummy nodes heading to pickups
        index = manager.NodeToIndex(node)
        # dummy nodes can only get to one node
        tt = expanded_mm.loc[node,:]
        bool_idx = tt > 0
        next_node = expanded_mm.index[bool_idx].max()
        tw = d.get_time_window(next_node)
        if tw[0]>0 :
            # adjust to allow for breaks
            # earliest time window must allow for 10 hour break
            tw = (tw[0]- 600,tw[1])

        # print('dummy node time window',node,index)
        time_dimension.CumulVar(index).SetRange(int(tw[0]),int(tw[1]))
        routing.AddToAssignment(time_dimension.SlackVar(index))

    # Add time window constraints for each vehicle start node
    # and 'copy' the slack var in the solution object (aka Assignment) to print it
    for vehicle in vehicles.vehicles:
        vehicle_id = vehicle.index
        index = routing.Start(vehicle_id)
        # not really needed unless different from 0, horizon
        time_dimension.CumulVar(index).SetRange(vehicle.time_window[0],
                                                vehicle.time_window[1])
        routing.AddToAssignment(time_dimension.SlackVar(index))



    # [START breaks logic]
    print('apply break rules')
    d.apply_breaks_rules(vehicles,expanded_mm,routing)

    # did it work?
    print('breaks done')

    # prevent impossible next nodes
    print('remove impossible connections from solver')
    for onode in expanded_mm.index:
        o_idx = manager.NodeToIndex(onode)
        for dnode in expanded_mm.index:
            if onode == dnode:
                continue
            if np.isnan(expanded_mm.loc[onode,dnode]):
                # cannot connect, to prevent this combo
                d_idx = manager.NodeToIndex(dnode)
                if routing.NextVar(o_idx).Contains(d_idx):
                    # print('remove link from',onode,'to',dnode)
                    routing.NextVar(o_idx).RemoveValue(d_idx)
    print('done with RemoveValue calls')


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
            p = penalty #break_penalty
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
        SO.print_solution(d,expanded_m,vehicles,manager,routing,assignment,args.horizon)


    else:
        print('assignment failed')



if __name__ == '__main__':
    main()
