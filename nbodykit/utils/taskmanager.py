import logging
import os
import tempfile
from mpi4py import MPI
from nbodykit.extensionpoints import Algorithm, algorithms

logger = logging.getLogger('taskmanager')

#------------------------------------------------------------------------------
# tools
#------------------------------------------------------------------------------        
def split_ranks(N_ranks, N_chunks):
    """
    Divide the ranks into N chunks, removing the master (0) rank
    
    Parameters
    ----------
    N_ranks : int
        the total number of ranks available
    N_chunks : int
        the number of chunks to split the ranks into
    """
    seq = range(1, N_ranks)
    avg = int((N_ranks-1) // N_chunks)
    remainder = (N_ranks-1) % N_chunks

    start = 0
    end = avg
    for i in range(N_chunks):
        if remainder:
            end += 1
            remainder -= 1
        yield i, seq[start:end]
        start = end
        end += avg
        
def enum(*sequential, **named):
    enums = dict(zip(sequential, range(len(sequential))), **named)
    return type('Enum', (), enums)
    
                    
def replacements_from_file(value):
    """
    Provided an existing file name, read the file into 
    a dictionary. The keys are interpreted as the string format name, 
    and the values are a list of values to iterate over for each job
    """
    if not os.path.exists(value):
        raise RuntimeError("for `replacements_from_file`, file `%s` does not exist" %value)
    toret = {}
    execfile(value, globals(), toret)
    return toret
        
def tasks_parser(value):
    """
    Given a string of the format ``key tasks``, split the string and then
    try to parse the ``tasks``, by first trying to evaluate it, and then
    simply splitting it and interpreting the results as the tasks. 
    
    The general use cases are: 
    
    1) "box: range(2)" -> key = `box`, tasks = `[0, 1]`
    2) "box: ['A', 'B' 'C']" -> key = `box`, tasks = `['A', 'B', 'C']`
    """
    import yaml
    
    try:
        fields = yaml.load(value)
        keys = list(fields.keys())
        if len(fields) != 1:
            raise Exception
    except:
        raise ValueError("specify iteration tasks via the format: ``-i key: [task1, task2]``")
    
    key = keys[0]
    if isinstance(fields[key], list):
        parsed = fields[key]
    else:
        # try to eval into a list
        try:
            parsed = eval(fields[key])
            if not isinstance(parser, list):
                raise ValueError("result of `eval` on iteration string should be list" %(fields[key]))
        except:
            raise ValueError("tried but failed to `eval` iteration string `%s`" %(fields[key]))

    return [key, parsed]

#------------------------------------------------------------------------------
# task manager
#------------------------------------------------------------------------------
class TaskManager(object):
    """
    Task manager for running a set of `Algorithm` computations,
    possibly in parallel using MPI
    """
    def __init__(self, comm, algorithm_name, config, workers, task_dims, 
                    task_values, log_level=logging.INFO, extras={}):
        """
        Parameters
        ----------
        task_function : callable
            the function to call for each task; arguments should be 
            an :py:class: `argparse.Namespace` and optionally
            a :py:class: `mpi4py.MPI.Intracomm` as the ``comm`` 
            keyword
        config : argparse.Namespace
            the namespace specifying the `TaskManager` configuration.
            see source code for ``TaskManager.parse_args`` for
            details on attributes
        task_parser : argparse.ArgumentParser
            the argument parser for the ``task_function`` that will 
            return the parameters to be passed to ``task_function``
            for each task
        comm : mpi4py.MPI.Intracomm, optional
            the global communicator, which will possibly be split
            and to use multiple comms across several nodes. If `None`,
            ``MPI.COMM_WORLD`` is used
        """
        logger.setLevel(log_level)
        
        self.algorithm_name  = algorithm_name
        self.algorithm_class = getattr(algorithms, algorithm_name) 
        self.template        = open(config, 'r').read()
        self.workers         = workers
        self.task_dims       = task_dims
        self.task_values     = task_values
        self.extras           = extras
        
        self.comm = comm
        self.size = comm.size
        self.rank = comm.rank
        
        # crash if we only have one process or one worker
        if self.size <= self.workers:
            args = (self.size, self.workers+1, self.workers)
            raise ValueError("only have %d ranks; need at least %d to use the desired %d workers" %args)
        
    @classmethod
    def create(cls, comm=None, desc=None):
        """
        Parse the task manager and return the ``TaskManager`` instance
        """
        import inspect 
        
        if comm is None: comm = MPI.COMM_WORLD
        args_dict = cls.parse_args(desc)
        args_dict['comm'] = comm
        
        # inspect the __init__ function
        args, varargs, varkw, defaults = inspect.getargspec(cls.__init__)
        
        # determine the required arguments
        args = args[1:] # remove 'self'
        if defaults:
            required = args[:-len(defaults)]
        else:
            required = args
            
        # get the args, kwargs to pass to __init__
        fargs = tuple(args_dict[p] for p in required)
        fkwargs = {}
        if defaults:
            for i, p in enumerate(defaults):
                name = args[-len(defaults)+i]
                fkwargs[name] = args_dict.get(name, defaults[i])
        
        return cls(*fargs, **fkwargs)
        
    @classmethod
    def parse_args(cls, desc=None):
        """
        Parse command-line arguments that are needed to initialize a 
        `TaskManager` class
        
        Parameters
        ----------
        desc : str, optional
            the description of to use for this parser
        """
        import argparse
        import itertools
        
        # parse
        parser = argparse.ArgumentParser(description=desc) 
        
        # first argument is the algorithm name
        h = 'the name of the `Algorithm` to run in batch mode'
        valid_algorithms = list(vars(algorithms).keys())  
        parser.add_argument(dest='algorithm_name', choices=valid_algorithms, help=h)  
        
        # the number of independent workers
        h = "the number of independent works that will run tasks in parallel"        
        parser.add_argument('workers', type=int, help=h)
    
        # now do the required named arguments
        required_named = parser.add_argument_group('required named arguments')
        
        # specify the tasks along one dimension 
        h =  """given a string of the format ``key: tasks``, split the string and then
                try to parse the ``tasks``, by first trying to evaluate it, and then
                simply splitting it and interpreting the results as the tasks. 
        
                The general use cases are: 
        
                1) "box: range(2)" -> key = `box`, tasks = `[0, 1]`
                2) "box: [A, B, C]" -> key = `box`, tasks = `['A', 'B', 'C']`
                
                If multiple options passed with `-i` flag, then the total tasks to 
                perform will be the product of the tasks lists passed"""
        required_named.add_argument('-i', dest='tasks', action='append', 
                type=tasks_parser, required=True, help=h)
    
        # the template config file
        h = """the name of the template config file (using YAML synatx) that 
                provides the `Algorithm` parameters; the file should use 
                ``string.format`` syntax to indicate which variables will be 
                updated for each task, i.e., an input file could be specified 
                as 'input/DataSource_box{box}.dat', if `box` were one of the task 
                dimensions"""
        required_named.add_argument('-c', '--config', type=str, help=h)
    
        # read any extra string replacements from file
        h = """file providing extra string replaces, with lines of the form 
                 `tag = ['tag1', 'tag2']`; if the keys match keywords in the 
                 template param file, the file with be updated with
                 the `ith` value for the `ith` task"""
        parser.add_argument('--extra', dest='extras', default={}, type=replacements_from_file, help=h)
    
        h = "set the logging output to debug, with lots more info printed"
        parser.add_argument('--debug', help=h, action="store_const", dest="log_level", 
                            const=logging.DEBUG, default=logging.INFO)
                                
        args = parser.parse_args()
        
        # format the tasks, taking the product of multiple task lists
        keys = []; values = []
        for [key, tasks] in args.tasks:
            keys.append(key)
            values.append(tasks)

        # take the product
        if len(keys) > 1:
            values = list(itertools.product(*values))
        else:
            values = values[0]
            
        # save
        args.task_dims = keys
        args.task_values = values
        
        return vars(args)
    
    def _initialize_pool_comm(self):
        """
        Internal function that initializes the `MPI.Intracomm` used by the 
        pool of workers. This will be passed to the task function and used 
        in task computation
        """
        # split the ranks
        self.pool_comm = None
        chain_ranks = []
        color = 0
        worker_count = 0
        for i, ranks in split_ranks(self.size, self.workers):
            chain_ranks.append(ranks[0])
            if self.rank in ranks: color = i+1
            worker_count += len(ranks)
        
        if worker_count != self.size-1:
            args = (worker_count, self.size-1)
            raise RuntimeError("mismatch between worker count (%d) and spawned worker processes (%d)" %args)
        self.pool_comm = self.comm.Split(color, 0)
        
    def run_all(self):
        """
        Run all of the tasks
        """    
        # define MPI message tags
        tags = enum('READY', 'DONE', 'EXIT', 'START')
        status = MPI.Status()
         
        # make the pool comm
        self._initialize_pool_comm()
    
        # the total numbe rof tasks
        num_tasks = len(self.task_values)
    
        # master distributes the tasks
        if self.rank == 0:
        
            # initialize
            task_index = 0
            closed_workers = 0
        
            # loop until all workers have finished with no more tasks
            logger.info("master starting with %d worker(s) with %d total tasks" %(self.workers, num_tasks))
            while closed_workers < self.workers:
                data = self.comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)
                source = status.Get_source()
                tag = status.Get_tag()
            
                # worker is ready, so send it a task
                if tag == tags.READY:
                    if task_index < num_tasks:
                        self.comm.send(task_index, dest=source, tag=tags.START)
                        logger.debug("sending task `%s` to worker %d" %(str(self.task_values[task_index]), source))
                        task_index += 1
                    else:
                        self.comm.send(None, dest=source, tag=tags.EXIT)
                elif tag == tags.DONE:
                    results = data
                    logger.debug("received result from worker %d" %source)
                elif tag == tags.EXIT:
                    closed_workers += 1
                    logger.debug("worker %d has exited, closed workers = %d" %(source, closed_workers))
    
        # worker processes wait and execute single jobs
        else:
            if self.pool_comm.rank == 0:
                args = (self.rank, MPI.Get_processor_name(), self.pool_comm.size)
                logger.info("pool master rank is %d on %s with %d processes available" %args)
            while True:
                itask = -1
                tag = -1
        
                # have the master rank of the pool ask for task and then broadcast
                if self.pool_comm.rank == 0:
                    self.comm.send(None, dest=0, tag=tags.READY)
                    itask = self.comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
                    tag = status.Get_tag()
                itask = self.pool_comm.bcast(itask)
                tag = self.pool_comm.bcast(tag)
        
                # do the work here
                if tag == tags.START:
                    result = self._run_algorithm(itask)
                    self.pool_comm.Barrier() # wait for everyone
                    if self.pool_comm.rank == 0:
                        self.comm.send(result, dest=0, tag=tags.DONE) # done this task
                elif tag == tags.EXIT:
                    break

            self.pool_comm.Barrier()
            if self.pool_comm.rank == 0:
                self.comm.send(None, dest=0, tag=tags.EXIT) # exiting
    
        # free and exit
        logger.debug("rank %d process finished" %self.rank)
        self.comm.Barrier()
        if self.rank == 0:
            logger.info("master is finished; terminating")
            self.pool_comm.Free()
            
            
    def _run_algorithm(self, itask):
        """
        Run the algorithm once, using the parameters specified for this task
        iteration specified by `itask`
    
        Parameters
        ----------
        itask : int
            the integer index of this task
        """
        task = self.task_values[itask]

        # if you are the pool's root, write out the temporary parameter file
        this_config = None
        if self.pool_comm.rank == 0:
            # extract the keywords that we need to format from template file
            kwargs = [kw for _, kw, _, _ in self.template._formatter_parser() if kw]
            
            # initialize a temporary file
            with tempfile.NamedTemporaryFile(delete=False) as ff:
                
                this_config = ff.name
                logger.debug("creating temporary file: %s" %this_config)
                
                # key/values for this task 
                if len(self.task_dims) == 1:
                    possible_kwargs = {self.task_dims[0] : task}
                else:
                    possible_kwargs = dict(zip(self.task_dims, task))
                    
                # any extra key/value pairs for this tasks
                if self.extras is not None:
                    for k in self.extras:
                        possible_kwargs[k] = self.extras[k][itask]
                        
                # do the string formatting if the key is present in template
                valid = {k:v for k,v in possible_kwargs.iteritems() if k in kwargs}
                ff.write(self.template.format(**valid))
        
        # bcast the file name to all in the worker pool
        this_config = self.pool_comm.bcast(this_config, root=0)

        # configuration file passed via -c
        params, extra = Algorithm.parse_known_yaml(self.algorithm_name, this_config)
        
        # output is required
        output = getattr(extra, 'output', None)
        if output is None:
            raise ValueError("argument `output` is required in config file")
            
        # initialize the algorithm and run
        alg = self.algorithm_class(self.pool_comm, **vars(params))
        result = alg.run()
        alg.save(output, result)

        # remove temporary files
        if self.pool_comm.rank == 0:
            if os.path.exists(this_config): 
                logger.debug("removing temporary file: %s" %this_config)
                os.remove(this_config)
    
        
        
        
        
