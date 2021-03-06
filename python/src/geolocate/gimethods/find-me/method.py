##
#  Copyright (c) 2015, Tyler Finethy
#
#  All rights reserved. See LICENSE file for details
##

"""
A twitter-based geolocation inferenced method based on "Find Me If You Can:
Improving Geographical Prediction with Social and Spatial Proximity," by Backstrom, Sun and Marlow

Author: Tyler Finethy
Date Created: June 2014
"""

from collections import defaultdict
import math
import os
import os.path
import logging
import gzip
import zen

import numpy as np
from scipy.optimize import curve_fit

from haversine import haversine
from geolocate import GIMethod, GIModel

try:
    import cPickle as pickle
except:
    import pickle

logger = logging.getLogger(__name__)

class FindMeMethod(GIMethod):
    """
    TextBasedMethod extends GIMethod
    """
    train_called = False
    settings = None
    dataset = None
    model_dir = None
    load_called = False

    @staticmethod
    def clear():
        FindMeMethod.train_called = False
        FindMeMethod.settings = None
        FindMeMethod.dataset = None
        FindMeMethod.model_dir = None
        FindMeMethod.load_called = False

    def __init__(self):
        pass

    def train_model(self,settings,dataset,model_dir=None):
        """
        Creates a TextBasedModel object classifier and saves the object as a pickled file
        """
        FindMeMethod.train_called = True
        FindMeMethod.settings = settings
        FindMeMethod.dataset = dataset

        #running the Backstrom find me model, and storing the network
        network = FindMe().get_network()
        user_id_to_location = {}
        for user_id in network.nodes_iter():
            try:
                location = network.node_data(user_id)
                if not location is None:
                    user_id_to_location[user_id] = location
            except KeyError:
                pass            

        #storing the network as a findmemodel class...
        model = FindMeModel(user_id_to_location)


        if model_dir:
            print "saving model"
            FindMeMethod.model_dir = model_dir
            filename = os.path.join(model_dir, "user-to-lat-lon.tsv.gz")

            fh = gzip.open(filename, 'w')
            for user_id, loc in user_id_to_location.iteritems():
                fh.write("%s\t%s\t%s\n" % (user_id, loc[0], loc[1]))
            fh.close()


        return model


    def load_model(self,model_dir=None,settings=None):
        """
        Loads a TextBasedModel object classifier from a pickled file object
        """
        self.load_called = True
        self.model_dir = model_dir
        path_settings = os.path.join(self.model_dir, "user-to-lat-lon.tsv.gz")

        user_id_to_location = {}
        fh = gzip.open(path_settings, "r")
        for line in fh:
            cols = line.split("\t")
            user_id_to_location[cols[0]] = (float(cols[1]), float(cols[2]))
        fh.close()

        if settings:
            self.settings = settings

        return FindMeModel(user_id_to_location)


class FindMeModel(GIModel):
    """
    TextBasedModel extends GIModel
    """
    num_posts_inferred = 0
    num_users_inferred = 0

    @staticmethod
    def clear():
        FindMeModel.num_users_inferred = 0
        FindMeModel.num_posts_inferred = 0

    def __init__(self,user_id_to_location):
        """
        Initializes the class based on the model/classifier created by TextBasedMethod
        """
        self.user_id_to_location = user_id_to_location
        return

    def infer_post_location(self,post):
        """
        Infers the class given the text from a tweet or post.
        """
        FindMeModel.num_posts_inferred += 1
        userID = post['user']['id_str']
        if FindMeModel.num_posts_inferred % 100000 == 0:
            logger.debug('Backstrom has inferred the location of %d users' % FindMeModel.num_posts_inferred)
        try:
            return self.user_id_to_location[userID]
        except KeyError:
            return None

    def infer_posts_by_user(self,posts):
        """
        Infers classes given the text from multiple tweets or posts.
        """
        FindMeModel.num_users_inferred += 1
        return [self.infer_post_location(post) for post in posts]


class FindMe(object):
    def __init__(self):
        #G represents the mention network zen graph
        logger.debug('Loading mention network')
        self.G = FindMeMethod.dataset.bi_mention_network()
        self.nodes_with_data = set()
        self.nodes_without_data = set()

        self.nodes = set(self.G.nodes())
        logger.debug('Fixing mention network')
        self.store_location_data()
        logger.debug('Mention network fixed!')
        self.locations = []
        self.location_probabilities = defaultdict(list)

        ##the coefficients as described in the paper, these are just a place holder.
        self.a = 0.0019
        self.b = 0.196
        self.c = -0.015

        #recompute the coefficients according to the mention-network provided
        #purposes, this information may be important.
        #
        #This was a major finding in the original paper <-> Comparing and
        #contrasting the coefficients found would be sensible.
        logger.debug('Computing coefficients')
        self.compute_coefficients()
        logger.debug('Calculating locations')
        self.calculate_location()
        logger.debug('Done calculating locations')

    def compute_coefficients(self):
        """
        From the paper:
        Probability of friendship as a function of distance.
        By computing the number of pairs of individuals at varying distances,
        along with the number of friends at those distances,
        we are able to compute the probability of two people at distance d knowing each other.
        We see here that it is a reasonably good fit to a power-law with exponent near  1.
        """
        def func_to_fit(x,a,b,c):
            return a * (x + b)**c

        size = len(self.nodes_with_data)
        fitting_dictionary = defaultdict(float)
        
        
        logger.debug('Inferring coefficients from %d users with locations' % size)


        # Sanity check to ensure that at least one node has location data
        if size == 0:
            return

        additive = (1.0/size) #precompute the normalized additive amount

        for node in self.nodes_with_data:
            location_u = self.G.node_data(node)
            for neighbor in self.G.neighbors_iter(node):
                location_v = self.G.node_data(neighbor)
                if not location_v: continue
                distance = haversine(location_u,location_v,miles=True)
                # Backstrom et al. bucket the distances in 1/10 mile increments
                # which we do here
                bucketed_distance = round(distance, 1)
                fitting_dictionary[bucketed_distance] += additive

        x = np.array(sorted([key for key in fitting_dictionary]))
        y = np.array([fitting_dictionary[key] for key in x])

        ##curve_fit, if this seems problematic finding a different fitting
        ##function may be necessary..?  works by Levenberg-Marquardt algorithm
        ##(LMA) used to solve non-linear least square problems so it should be
        ##quite fitting ;)
        solutions = curve_fit(func_to_fit,x,y,maxfev=100000)[0]
        self.a = solutions[0]
        self.b = solutions[1]
        self.c = solutions[2]

        logger.debug('Found coefficients a=%f, b=%f, c=%f' % (self.a, self.b, self.c))
        return

    def calculate_probability(self, l_u, l_v):
        """
        Calculates the probability of the edge being present given the distance
        between user u and neighbor v, l_u indicates the location of user u and
        l_v indicates the location of user v (tuples containing latitude and
        longitude).
        """
        return self.a * (abs(haversine(l_u,l_v,miles=True)) + self.b)**(self.c)

    def calculate_location(self):
        """
        Iterates through the neighbors of each user, calculating the probability
        of those neighbors being near each other with all other neighbors. The
        most-likely location is the highest probability lat/lon and is reported
        as the unknown-location users location.
        """
        num_users_located = 0
        for node in self.nodes_without_data:
            best_log_probability = 0.0
            for neighbor_u in self.G.neighbors_iter(node):
                if neighbor_u not in self.nodes_with_data: continue
                l_u = self.G.node_data(neighbor_u)
                log_probability = 0.0
                best_location = self.G.node_data(neighbor_u)

                for neighbor_v in self.G.neighbors_iter(neighbor_u):
                    if neighbor_v not in self.nodes_with_data: continue
                    l_v = self.G.node_data(neighbor_v)
                    # as per the article, "it is important to do all the
                    # calculations adding logarithms, instead of multiplying
                    # probabilities to avoid underflow.
                    l_v = self.G.node_data(neighbor_v)
                    plu_lv = self.calculate_probability(l_u,l_v)
                    log_gamma_lu = math.log(plu_lv) - math.log(1-plu_lv) #switched to log arithmetic to remove chance of causing underflow
                    log_probability += log_gamma_lu

                if (log_probability >= best_log_probability):
                    best_location = self.G.node_data(neighbor_u)
                    best_log_probability = log_probability
                    
                if best_location:
                    self.G.set_node_data(node,best_location)

            num_users_located += 1
            if num_users_located % 100000 == 0:
                logger.debug('Backstrom located %d/%d users so far' % 
                             (num_users_located, len(self.nodes_without_data)))

    def get_network(self):
        """
        Returns network for storage (model) purposes
        """
        return self.G

    def store_location_data(self):
        """

        """
        self.nodes = set(self.G.nodes())

        num_users_seen = 0
        for user_id, loc in FindMeMethod.dataset.user_home_location_iter():
            if loc[0] == 0 and loc[1] == 0:
                continue
            try:
                self.G.set_node_data(user_id, loc)
                self.nodes_with_data.add(user_id)
                num_users_seen += 1
                if num_users_seen % 100000 == 0:
                    logger.debug('Backstrom saw %d users' % num_users_seen)
            except KeyError:
                pass
            
        for user_id in self.nodes:
            if not user_id in self.nodes_with_data:
                self.nodes_without_data.add(user_id)

        logger.debug('Backstrom saw %d user with a home location and %d without' 
                     % (len(self.nodes_with_data), len(self.nodes_without_data)))


